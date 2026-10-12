"""Command-line entry point for bounded local product collection."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import __version__
from .collectors import SOURCES, crawl
from .feed import import_feed
from .pipeline import ingest_jsonl, ingest_records
from .quality import quality_report
from .removal import recheck_report
from .schema import utc_now
from .search import search_catalogue
from .storage import export_catalogue, load_run, open_database


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fashion-finder", description=__doc__)
    parser.add_argument("--version", action="version", version=f"fashion-finder-data {__version__}")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    commands = parser.add_subparsers(dest="command", required=True)
    sources = commands.add_parser("sources", help="Show configured Hungarian storefronts")
    sources.add_argument("action", choices=["list"])
    collect = commands.add_parser("collect", help="Collect a bounded product sample")
    collect.add_argument("source", choices=[*SOURCES, "all"])
    collect.add_argument(
        "--limit", type=int, default=5, help="Maximum product pages per source (1–100)"
    )
    collect.add_argument(
        "--url", action="append", help="Starting category or product URL; one source only"
    )
    ingest = commands.add_parser("ingest", help="Replay a raw JSONL snapshot")
    ingest.add_argument("source", choices=SOURCES)
    ingest.add_argument("path", type=Path)
    feed = commands.add_parser("import-feed", help="Import an authorized local product CSV")
    feed.add_argument("source", choices=SOURCES)
    feed.add_argument("path", type=Path)
    export = commands.add_parser("export", help="Export the stored catalogue")
    export.add_argument("path", type=Path)
    export.add_argument("--source", choices=SOURCES)
    report = commands.add_parser("report", help="Show one collection run")
    report.add_argument("run_id")
    search = commands.add_parser("search", help="Search the local HUF catalogue")
    search.add_argument("--query", help="Words in title, brand, or description")
    search.add_argument("--source", choices=SOURCES)
    search.add_argument("--category", help="Exact normalized category, such as coats")
    search.add_argument("--max-price", help="Maximum price in the selected currency")
    search.add_argument("--currency", default="HUF", help="Exact currency (default: HUF)")
    search.add_argument(
        "--sort", choices=["price-asc", "price-desc", "recent"], default="price-asc"
    )
    search.add_argument("--limit", type=int, default=20, help="Maximum results (1–100)")
    quality = commands.add_parser("quality", help="Report local offer freshness and completeness")
    quality.add_argument("--stale-after-days", type=int, default=7)
    quality.add_argument("--output", type=Path, help="Create a JSON report file")
    recheck = commands.add_parser("recheck", help="List offers needing direct source review")
    recheck.add_argument("--source", choices=SOURCES)
    recheck.add_argument("--stale-after-days", type=int, default=7)
    recheck.add_argument("--limit", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command == "sources":
        for source in SOURCES.values():
            print(f"{source.key}\tHU\t{source.category_url}")
        return 0
    if args.command == "collect":
        if not 1 <= args.limit <= 100:
            parser.error("--limit must be between 1 and 100")
        if args.source == "all" and args.url:
            parser.error("--url can only be used with one source")
    engine = open_database(args.data_dir / "fashion_finder.sqlite")
    if args.command == "collect":
        selected = list(SOURCES.values()) if args.source == "all" else [SOURCES[args.source]]
        started_at = utc_now().isoformat()
        state = crawl(selected, args.limit, {args.source: args.url} if args.url else None)
        reports = [
            ingest_records(
                engine,
                args.data_dir,
                source.key,
                state.records.get(source.key, []),
                state.errors.get(source.key, []),
                started_at=started_at,
                discovered=state.discovered.get(source.key, 0),
            )
            for source in selected
        ]
        export_catalogue(engine, args.data_dir / "exports" / "catalogue.jsonl")
        export_catalogue(engine, args.data_dir / "exports" / "catalogue.csv")
        for report in reports:
            print(json.dumps(report, ensure_ascii=False))
        return 0 if all(report["status"] == "success" for report in reports) else 1
    if args.command == "ingest":
        report = ingest_jsonl(engine, args.data_dir, args.path, args.source)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["status"] == "success" else 1
    if args.command == "import-feed":
        try:
            report = import_feed(engine, args.data_dir, args.path, args.source)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(report, ensure_ascii=False))
        export_catalogue(engine, args.data_dir / "exports" / "catalogue.jsonl")
        export_catalogue(engine, args.data_dir / "exports" / "catalogue.csv")
        return 0 if report["status"] == "success" else 1
    if args.command == "export":
        count = export_catalogue(engine, args.path, args.source)
        print(f"Exported {count} offers to {args.path}")
        return 0
    if args.command == "report":
        report = load_run(engine, args.run_id)
        if report is None:
            print(f"Run not found: {args.run_id}")
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "search":
        try:
            max_price = Decimal(args.max_price) if args.max_price is not None else None
            results = search_catalogue(
                engine,
                query=args.query,
                source=args.source,
                category=args.category,
                currency=args.currency.strip().upper(),
                max_price=max_price,
                sort=args.sort,
                limit=args.limit,
            )
        except (InvalidOperation, ValueError) as exc:
            parser.error(str(exc))
        for item in results:
            print(json.dumps(item, ensure_ascii=False))
        return 0
    if args.command == "quality":
        try:
            result = quality_report(engine, stale_after_days=args.stale_after_days)
            body = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as stream:
                    stream.write(body)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(body, end="")
        return 0
    if args.command == "recheck":
        try:
            result = recheck_report(
                engine,
                source_key=args.source,
                stale_after_days=args.stale_after_days,
                limit=args.limit,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

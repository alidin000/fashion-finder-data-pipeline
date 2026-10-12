"""Command-line entry point for bounded local product collection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__
from .collectors import SOURCES, crawl
from .pipeline import ingest_jsonl, ingest_records
from .schema import utc_now
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
    export = commands.add_parser("export", help="Export the stored catalogue")
    export.add_argument("path", type=Path)
    export.add_argument("--source", choices=SOURCES)
    report = commands.add_parser("report", help="Show one collection run")
    report.add_argument("run_id")
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Command-line entry point for bounded local product collection."""

from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import __version__
from .audit import audit_report
from .collection import (
    add_collect_arguments,
    add_scraper_commands,
    handle_runner_command,
    handle_scraper_command,
)
from .collectors import MAX_LIMIT
from .evaluation import evaluate_tasks
from .feed import import_feed, validate_feed
from .feed_mapping import map_xml_feed
from .image_fetch import fetch_images
from .image_index import build_image_index, search_indexed_text
from .pipeline import ingest_jsonl
from .practice.loader import MARKER_FILE as PRACTICE_MARKER
from .practice.loader import build_practice_index, load_practice_catalogue
from .product_text import build_product_text_index
from .quality import quality_report
from .ranking_evaluation import evaluate_rankings
from .readiness import readiness_report, write_audit_sample
from .removal import recheck_report
from .search import search_catalogue
from .source_policy import load_policies
from .sources import CRAWL_SOURCES, NON_RETAIL_SOURCES, SOURCES
from .storage import export_catalogue, load_run, open_database


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fashion-finder", description=__doc__)
    parser.add_argument("--version", action="version", version=f"fashion-finder-data {__version__}")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    commands = parser.add_subparsers(dest="command", required=True)
    sources = commands.add_parser("sources", help="Show configured Hungarian storefronts")
    sources.add_argument("action", choices=["list"])
    commands.add_parser("migrate", help="Adopt or upgrade the local SQLite catalogue for Django")
    collect = commands.add_parser("collect", help="Collect a bounded product sample")
    collect.add_argument("source", choices=[*CRAWL_SOURCES, "all"])
    collect.add_argument(
        "--limit",
        type=int,
        default=5,
        help=f"Maximum product offers per source (1–{MAX_LIMIT})",
    )
    collect.add_argument(
        "--url", action="append", help="Starting category or product URL; one source only"
    )
    collect.add_argument(
        "--browser", action="store_true", help="Render pages with Chromium before extraction"
    )
    collect.add_argument(
        "--browser-wait-ms",
        type=int,
        default=1500,
        help="Time to allow page scripts to render (0–10000; default: 1500)",
    )
    add_collect_arguments(collect)
    ingest = commands.add_parser("ingest", help="Replay a raw JSONL snapshot")
    ingest.add_argument("source", choices=SOURCES)
    ingest.add_argument("path", type=Path)
    feed = commands.add_parser("import-feed", help="Import an authorized local product CSV")
    feed.add_argument("source", choices=SOURCES)
    feed.add_argument("path", type=Path)
    validate = commands.add_parser("validate-feed", help="Check a feed without storing data")
    validate.add_argument("source", choices=SOURCES)
    validate.add_argument("path", type=Path)
    mapping = commands.add_parser("map-xml-feed", help="Map a local XML export to feed CSV")
    mapping.add_argument("source", choices=SOURCES)
    mapping.add_argument("xml_path", type=Path)
    mapping.add_argument("mapping_path", type=Path)
    mapping.add_argument("output_path", type=Path)
    mapping.add_argument(
        "--observed-at", help="ISO 8601 download time with timezone if XML has no timestamp"
    )
    mapping.add_argument(
        "--allow-partial",
        action="store_true",
        help="Write the valid rows and report the rejected ones instead of failing",
    )
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
        "--sort", choices=["price-asc", "price-desc", "recent", "relevance"], default="price-asc"
    )
    search.add_argument("--limit", type=int, default=20, help="Maximum results (1–100)")
    quality = commands.add_parser("quality", help="Report local offer freshness and completeness")
    quality.add_argument("--stale-after-days", type=int, default=7)
    quality.add_argument("--output", type=Path, help="Create a JSON report file")
    recheck = commands.add_parser("recheck", help="List offers needing direct source review")
    recheck.add_argument("--source", choices=SOURCES)
    recheck.add_argument("--stale-after-days", type=int, default=7)
    recheck.add_argument("--limit", type=int, default=100)
    policy_default = os.environ.get("FASHION_FINDER_SOURCE_POLICIES")
    readiness = commands.add_parser(
        "readiness", help="Measure one launch category against release gates"
    )
    readiness.add_argument("--category", required=True)
    readiness.add_argument(
        "--policy", type=Path, default=Path(policy_default) if policy_default else None
    )
    audit = commands.add_parser("audit-sample", help="Create a reproducible manual-review CSV")
    audit.add_argument("--category", required=True)
    audit.add_argument("--source", choices=SOURCES, required=True)
    audit.add_argument(
        "--policy", type=Path, default=Path(policy_default) if policy_default else None
    )
    audit.add_argument("--count", type=int, default=30)
    audit.add_argument(
        "--all-stored", action="store_true", help="Include nonpublishable offers for internal QA"
    )
    audit.add_argument("--output", type=Path, required=True)
    audit_results = commands.add_parser(
        "audit-report", help="Validate completed review CSVs against the current catalogue"
    )
    audit_results.add_argument("paths", type=Path, nargs="+")
    audit_results.add_argument("--category", required=True)
    audit_results.add_argument(
        "--policy", type=Path, default=Path(policy_default) if policy_default else None
    )
    audit_results.add_argument("--output", type=Path, help="Create a JSON report file")
    evaluation = commands.add_parser(
        "evaluate-tasks", help="Score consented shopper-task labels against current offers"
    )
    evaluation.add_argument("path", type=Path)
    evaluation.add_argument("--category", required=True)
    evaluation.add_argument(
        "--policy", type=Path, default=Path(policy_default) if policy_default else None
    )
    evaluation.add_argument("--output", type=Path, help="Create a JSON report file")
    rankings = commands.add_parser(
        "evaluate-rankings", help="Replay private tasks against local text and image search"
    )
    rankings.add_argument("path", type=Path)
    rankings.add_argument("--category", required=True)
    rankings.add_argument(
        "--policy", type=Path, default=Path(policy_default) if policy_default else None
    )
    model_default = os.environ.get("FASHION_FINDER_IMAGE_MODEL_PATH")
    text_default = os.environ.get("FASHION_FINDER_TEXT_MODEL_PATH")
    tokenizer_default = os.environ.get("FASHION_FINDER_TOKENIZER_PATH")

    def add_model_paths(command: argparse.ArgumentParser, *, text: bool) -> None:
        command.add_argument(
            "--model-path", type=Path, default=Path(model_default) if model_default else None
        )
        if text:
            command.add_argument(
                "--text-model-path",
                type=Path,
                default=Path(text_default) if text_default else None,
            )
            command.add_argument(
                "--tokenizer-path",
                type=Path,
                default=Path(tokenizer_default) if tokenizer_default else None,
            )

    add_model_paths(rankings, text=True)
    rankings.add_argument("--output", type=Path, help="Create a JSON report file")
    look = commands.add_parser(
        "search-look", help="Find indexed product photos that match a description"
    )
    look.add_argument("query")
    look.add_argument(
        "--policy", type=Path, default=Path(policy_default) if policy_default else None
    )
    add_model_paths(look, text=True)
    for option in ("--category", "--colour", "--size", "--source"):
        look.add_argument(option)
    look.add_argument("--max-price", type=Decimal)
    translation_default = os.environ.get("FASHION_FINDER_TRANSLATION_MODEL_DIR")
    look.add_argument(
        "--translate", choices=["none", "dictionary", "dictionary+mt", "mt"], default=None
    )
    look.add_argument(
        "--translation-model-dir",
        type=Path,
        default=Path(translation_default) if translation_default else None,
    )
    product_text = commands.add_parser(
        "index-product-text",
        help="Embed short English descriptions of indexed offers for look search",
    )
    product_text.add_argument(
        "--policy", type=Path, default=Path(policy_default) if policy_default else None
    )
    add_model_paths(product_text, text=True)
    images = commands.add_parser(
        "index-images", help="Build a local image-search experiment from permitted files"
    )
    images.add_argument("manifest", type=Path)
    images.add_argument(
        "--policy", type=Path, default=Path(policy_default) if policy_default else None
    )
    add_model_paths(images, text=False)
    images.add_argument(
        "--aspect",
        choices=("squash", "pad", "center-crop"),
        default="squash",
        help="How photos become the model's square input (ONNX models only)",
    )
    images.add_argument(
        "--flip-tta", action="store_true", help="Average each photo with its mirror image"
    )
    fetch = commands.add_parser(
        "fetch-images", help="Download product photos of stored offers for index-images"
    )
    fetch.add_argument(
        "--source", choices=[key for key in SOURCES if key not in NON_RETAIL_SOURCES]
    )
    fetch.add_argument(
        "--policy", type=Path, default=Path(policy_default) if policy_default else None
    )
    fetch.add_argument("--limit", type=int, default=100, help="Maximum offers (1–20000)")
    fetch.add_argument("--per-offer", type=int, default=2, help="Photos per offer (1–8)")
    fetch.add_argument(
        "--delay", type=float, default=2.0, help="Seconds between requests (0.5–60; default 2)"
    )
    fetch.add_argument(
        "--output-dir", type=Path, help="Photo and manifest folder (default: <data-dir>/images)"
    )
    fetch.add_argument(
        "--revalidate-days",
        type=float,
        help="Re-check photos fetched longer ago with ETag/Last-Modified (unchanged ones stay)",
    )
    add_scraper_commands(commands)
    practice = commands.add_parser(
        "load-practice-catalogue",
        help="Load the public-dataset practice catalogue into its own data directory",
    )
    practice.add_argument("practice_dir", type=Path)
    practice.add_argument("--limit", type=int, help="Products, balanced across categories")
    practice.add_argument(
        "--build-index",
        action="store_true",
        help="Also encode up to 2000 photos per run into the image index; rerun until complete",
    )
    add_model_paths(practice, text=False)
    metrics = commands.add_parser("analytics-report", help="Show anonymous daily event totals")
    metrics.add_argument("--days", type=int, default=7)
    metrics.add_argument("--output", type=Path, help="Create a JSON report file")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command == "sources":
        for source in SOURCES.values():
            start = source.category_url or (source.sitemaps[0] if source.sitemaps else "")
            print(f"{source.key}\tHU\t{start or 'feed-only'}\t{source.status}")
        return 0
    if args.command == "validate-feed":
        try:
            result = validate_feed(args.path, args.source)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "valid" else 1
    if args.command == "map-xml-feed":
        try:
            result = map_xml_feed(
                args.source,
                args.xml_path,
                args.mapping_path,
                args.output_path,
                observed_at=args.observed_at,
                allow_partial=args.allow_partial,
            )
        except (OSError, TypeError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "collect":
        if not 1 <= args.limit <= MAX_LIMIT:
            parser.error(f"--limit must be between 1 and {MAX_LIMIT}")
        if args.source == "all" and args.url:
            parser.error("--url can only be used with one source")
        if not 0 <= args.browser_wait_ms <= 10000:
            parser.error("--browser-wait-ms must be between 0 and 10000")
    # Before any collection runs: real retailer data must never land in a practice data dir.
    if (
        args.command in {"collect", "refresh", "ingest", "import-feed", "fetch-images"}
        and (args.data_dir / PRACTICE_MARKER).exists()
    ):
        parser.error("this data directory holds the practice catalogue; use another --data-dir")
    runner_result = handle_runner_command(args, parser)  # API mode needs no local catalogue
    if runner_result is not None:
        return runner_result
    if args.command == "load-practice-catalogue":
        try:
            result = load_practice_catalogue(args.data_dir, args.practice_dir, limit=args.limit)
            if args.build_index:
                result["index"] = build_practice_index(args.data_dir, model_path=args.model_path)
        except (OSError, TypeError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    engine = open_database(args.data_dir / "fashion_finder.sqlite")
    if args.command == "migrate":
        print(f"Migrated {engine.path}")
        return 0
    scraper_result = handle_scraper_command(args, engine, parser)
    if scraper_result is not None:
        return scraper_result
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
    if args.command == "index-images":
        try:
            if args.policy is None:
                raise ValueError("--policy is required to index images")
            result = build_image_index(
                engine,
                load_policies(args.policy),
                args.manifest,
                model_path=args.model_path,
                aspect=args.aspect,
                flip_tta=args.flip_tta,
            )
        except (OSError, TypeError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "search-look":
        try:
            if args.policy is None:
                raise ValueError("--policy is required for text-to-photo search")
            result = search_indexed_text(
                engine,
                load_policies(args.policy),
                args.query,
                category=args.category,
                colour=args.colour,
                size=args.size,
                source=args.source,
                max_price=args.max_price,
                model_path=args.model_path,
                text_model_path=args.text_model_path,
                tokenizer_path=args.tokenizer_path,
                translate=args.translate
                or ("dictionary+mt" if args.translation_model_dir else "dictionary"),
                translation_model_dir=args.translation_model_dir,
            )
        except (OSError, TypeError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "index-product-text":
        try:
            if args.policy is None:
                raise ValueError("--policy is required to describe indexed offers")
            result = build_product_text_index(
                engine,
                load_policies(args.policy),
                model_path=args.model_path,
                text_model_path=args.text_model_path,
                tokenizer_path=args.tokenizer_path,
            )
        except (OSError, TypeError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "fetch-images":
        try:
            if args.policy is None:
                raise ValueError("--policy is required: only indexable photos are fetched")
            result = fetch_images(
                engine,
                args.output_dir or args.data_dir / "images",
                load_policies(args.policy),
                source=args.source,
                limit=args.limit,
                per_offer=args.per_offer,
                delay=args.delay,
                revalidate_after_days=args.revalidate_days,
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] != "failed" else 1
    if args.command in {
        "readiness",
        "audit-sample",
        "audit-report",
        "evaluate-tasks",
        "evaluate-rankings",
    }:
        try:
            policies = load_policies(args.policy)
            if args.command == "readiness":
                result = readiness_report(engine, policies, args.category)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif args.command == "audit-sample":
                count = write_audit_sample(
                    engine,
                    policies,
                    args.category,
                    args.source,
                    args.output,
                    count=args.count,
                    all_stored=args.all_stored,
                )
                print(f"Wrote {count} audit offers to {args.output}")
            else:
                if args.command == "audit-report":
                    result = audit_report(engine, policies, args.category, args.paths)
                elif args.command == "evaluate-tasks":
                    result = evaluate_tasks(engine, policies, args.category, args.path)
                else:
                    result = evaluate_rankings(
                        engine,
                        policies,
                        args.category,
                        args.path,
                        model_path=args.model_path,
                        text_model_path=args.text_model_path,
                        tokenizer_path=args.tokenizer_path,
                    )
                body = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    with args.output.open("x", encoding="utf-8") as stream:
                        stream.write(body)
                print(body, end="")
        except (OSError, TypeError, ValueError) as exc:
            parser.error(str(exc))
        return 0
    if args.command == "analytics-report":
        from .analytics import metric_report

        try:
            body = (
                json.dumps(metric_report(engine, days=args.days), ensure_ascii=False, indent=2)
                + "\n"
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as stream:
                    stream.write(body)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(body, end="")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI wiring for discovery (`collect`), `refresh`, `runs`, `cycle-report` and the benchmark.

``cli.py`` registers these with a few lines; the logic lives here and in ``runner``,
``scheduler`` and ``benchmark`` so the scraper can change without touching the
shared CLI module.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .collectors import MAX_REQUESTS, crawl
from .crawl_state import CrawlStore, instant, product_key
from .pipeline import ingest_records, write_report
from .politeness import DEFAULT_DELAY_SECONDS, MIN_DELAY_SECONDS
from .runner import (
    ApiBackend,
    ApiError,
    DatabaseBackend,
    backend_from,
    publish,
    refresh_request_budget,
    run_discovery_api,
    run_refresh,
)
from .schema import RawProductRecord, RetailerOffer, utc_now
from .sources import CRAWL_SOURCES, DEFAULT_COLLECT_SOURCES, SOURCES, Source, user_agent
from .storage import Database, open_database

STATE_FILE = "crawl-state.sqlite"
DISCOVERY_MODES = ("auto", "listing", "sitemap")


def open_store(data_dir: Path) -> CrawlStore:
    return CrawlStore(data_dir / STATE_FILE)


def _runner_options(command: argparse.ArgumentParser, *, max_requests_default: int | None) -> None:
    command.add_argument(
        "--max-requests",
        type=int,
        default=max_requests_default,
        help=f"Request budget per source, robots.txt included (1–{MAX_REQUESTS})",
    )
    command.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Minimum seconds between requests to one host ({MIN_DELAY_SECONDS:g}–120;"
        " a longer robots.txt Crawl-delay or Retry-After wins)",
    )
    command.add_argument(
        "--contact-url",
        help="Contact URL announced in the User-Agent (default: $FASHION_FINDER_CONTACT_URL)",
    )
    command.add_argument(
        "--time-budget-minutes",
        type=float,
        default=None,
        help="Stop crawling cleanly after this long and report what is left",
    )
    command.add_argument(
        "--api-url",
        help="Send results to this Fashion Finder API (token in $FASHION_FINDER_API_TOKEN)"
        " instead of the local catalogue; default: $FASHION_FINDER_API_URL",
    )
    command.add_argument("--replay-dir", type=Path, help=argparse.SUPPRESS)  # tests only


def add_collect_arguments(collect: argparse.ArgumentParser) -> None:
    collect.add_argument(
        "--mode",
        choices=DISCOVERY_MODES,
        default="auto",
        help="auto: category listing, else sitemaps (discovery; use `refresh` for known offers)",
    )
    collect.add_argument(
        "--all-categories",
        action="store_true",
        help="Also crawl the source's curated category list (LPP shops)",
    )
    collect.add_argument(
        "--resume", action="store_true", help="Continue the source's interrupted run"
    )
    collect.add_argument(
        "--max-sitemaps", type=int, default=3, help="Child sitemaps fetched per run (1–50)"
    )
    _runner_options(collect, max_requests_default=200)


def add_scraper_commands(commands: Any) -> None:
    refresh = commands.add_parser(
        "refresh", help="Re-check due offers from their stored URLs (no site-wide crawl)"
    )
    refresh.add_argument(
        "--source",
        action="append",
        choices=list(CRAWL_SOURCES),
        help="Retailer to refresh (repeatable; default: every reviewed source)",
    )
    refresh.add_argument(
        "--limit", type=int, default=500, help="Due offers to check this run, all sources"
    )
    _runner_options(refresh, max_requests_default=None)
    runs = commands.add_parser("runs", help="Show recent discovery runs and refresh cycles")
    runs.add_argument("--source", choices=SOURCES)
    runs.add_argument("--limit", type=int, default=15)
    runs.add_argument("--run-id", help="Print one run's full report")
    cycle = commands.add_parser(
        "cycle-report", help="Refresh queue health: due, stale, failing, stretched intervals"
    )
    cycle.add_argument("--source", choices=SOURCES)
    cycle.add_argument("--json", action="store_true")
    benchmark = commands.add_parser(
        "benchmark-extraction",
        help="Sample product pages per retailer and measure extraction, speed and cycle time",
    )
    benchmark.add_argument("--source", action="append", choices=list(SOURCES))
    benchmark.add_argument("--per-source", type=int, default=5, help="Product pages per retailer")
    benchmark.add_argument("--output", type=Path, help="Also write the JSON report here")
    _runner_options(benchmark, max_requests_default=30)


def validate_runner_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.max_requests is not None and not 1 <= args.max_requests <= MAX_REQUESTS:
        parser.error(f"--max-requests must be between 1 and {MAX_REQUESTS}")
    floor = 0.0 if args.replay_dir else MIN_DELAY_SECONDS
    if not floor <= args.delay <= 120:
        parser.error(f"--delay must be between {floor:g} and 120 seconds")
    if args.time_budget_minutes is not None and not 1 <= args.time_budget_minutes <= 24 * 60:
        parser.error("--time-budget-minutes must be between 1 and 1440")
    if (
        getattr(args, "max_sitemaps", 1) is not None
        and not 1 <= getattr(args, "max_sitemaps", 1) <= 50
    ):
        parser.error("--max-sitemaps must be between 1 and 50")
    if getattr(args, "resume", False) and getattr(args, "url", None):
        parser.error("--resume continues the stored plan; it cannot take --url")
    try:
        user_agent(args.contact_url)
    except ValueError as exc:
        parser.error(str(exc))


def handle_runner_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int | None:
    """Commands that may run without a local catalogue (API mode). None: not handled."""
    if args.command not in {"refresh", "benchmark-extraction", "collect"}:
        return None
    validate_runner_args(args, parser)

    def engine() -> Database:
        return open_database(args.data_dir / "fashion_finder.sqlite")

    try:
        backend = backend_from(args, engine)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        if args.command == "refresh":
            selected = (
                [CRAWL_SOURCES[key] for key in dict.fromkeys(args.source)]
                if args.source
                else list(DEFAULT_COLLECT_SOURCES.values())
            )
            if not 1 <= args.limit <= 20_000:
                parser.error("--limit must be between 1 and 20000")
            if args.max_requests is None:
                args.max_requests = refresh_request_budget(args.limit, len(selected))
            if args.time_budget_minutes is None:
                args.time_budget_minutes = 100.0
            report = run_refresh(args, backend, selected)
            publish(report, "Fashion Finder refresh")
            return 0
        if args.command == "benchmark-extraction":
            from .benchmark import run_benchmark

            report = run_benchmark(args, backend)
            publish(report, "Fashion Finder extraction benchmark")
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            return 0
        if isinstance(backend, ApiBackend):
            selected = (
                list(DEFAULT_COLLECT_SOURCES.values())
                if args.source == "all"
                else [CRAWL_SOURCES[args.source]]
            )
            report = run_discovery_api(
                args, backend, selected, {args.source: args.url} if args.url else None
            )
            publish(report, "Fashion Finder discovery")
            return 0
        return run_collect(args, backend.engine, parser)
    except ApiError as exc:
        print(json.dumps({"status": "api_error", "detail": str(exc)}))
        return 1


def listing_root(record: RawProductRecord) -> str | None:
    """The category page an offer was listed on, without its page number."""
    from .discovery import without_page

    product_url = str(record.product.get("url") or "")
    if not product_url or product_key(product_url) == product_key(record.source_url):
        return None
    return without_page(record.source_url)


def run_collect(args: argparse.Namespace, engine: Database, parser: argparse.ArgumentParser) -> int:
    """Discovery into the local catalogue: plan, crawl, ingest, schedule, report."""
    selected = (
        list(DEFAULT_COLLECT_SOURCES.values())
        if args.source == "all"
        else [CRAWL_SOURCES[args.source]]
    )
    store = open_store(args.data_dir)
    started_at = utc_now().isoformat()
    if not args.resume:
        for source in selected:
            unfinished = store.unfinished_run(source.key)
            if unfinished:
                # Keep what the interrupted run already collected: ingest it as that run.
                print(
                    json.dumps(close_interrupted(engine, store, args.data_dir, source, unfinished))
                )
    run_ids: dict[str, str] = {}
    plans: dict[str, list] = {}
    resumed: dict[str, bool] = {}
    for source in selected:
        unfinished = store.unfinished_run(source.key) if args.resume else None
        if unfinished:
            from .collectors import PlanEntry

            pending = store.frontier(unfinished["run_id"], "pending")
            plans[source.key] = [
                PlanEntry(row["url"], row["kind"], row["priority"]) for row in pending
            ]
            run_ids[source.key] = unfinished["run_id"]
            resumed[source.key] = True
        else:
            run_ids[source.key] = store.begin_run(source.key, args.mode)
            resumed[source.key] = False
    known = {source.key: DatabaseBackend(engine).known(source.key) for source in selected}
    state = crawl(
        selected,
        args.limit,
        {args.source: args.url} if args.url else None,
        browser=args.browser,
        browser_wait_ms=args.browser_wait_ms,
        mode=args.mode,
        max_requests=args.max_requests,
        delay=args.delay,
        contact=args.contact_url,
        store=store,
        run_ids=run_ids,
        plans=plans,
        all_categories=args.all_categories,
        max_sitemaps=args.max_sitemaps,
        known=known,
        time_budget_minutes=args.time_budget_minutes,
        replay_dir=args.replay_dir,
    )
    reports = [
        finish_source_run(
            engine,
            store,
            args.data_dir,
            source,
            run_ids[source.key],
            state,
            started_at=started_at,
            mode=args.mode,
            resumed=resumed[source.key],
            contact=args.contact_url,
        )
        for source in selected
    ]
    store.close()
    from .storage import export_catalogue

    export_catalogue(engine, args.data_dir / "exports" / "catalogue.jsonl")
    export_catalogue(engine, args.data_dir / "exports" / "catalogue.csv")
    for report in reports:
        print(json.dumps(report, ensure_ascii=False, default=str))
    return 0 if all(report["status"] == "success" for report in reports) else 1


def _schedule_hook(engine: Database):
    from .scheduler import mark_observed

    def observed(record: RawProductRecord, offer: RetailerOffer, outcome: str) -> None:
        mark_observed(engine, record, offer)

    return observed


def close_interrupted(
    engine: Database, store: CrawlStore, data_dir: Path, source: Source, run: dict[str, Any]
) -> dict[str, Any]:
    """Ingest an interrupted run's spooled records and close it as ``interrupted``."""
    report = ingest_records(
        engine,
        data_dir,
        source.key,
        store.spooled(run["run_id"]),
        ["run interrupted before completion"],
        run_id=run["run_id"],
        started_at=run["started_at"],
        on_offer=_schedule_hook(engine),
    )
    report["mode"] = run["mode"]
    write_report(data_dir, report)
    store.finish_run(run["run_id"], "interrupted", report)
    return {"closed_interrupted_run": run["run_id"], "records_valid": report["records_valid"]}


def finish_source_run(
    engine: Database,
    store: CrawlStore,
    data_dir: Path,
    source: Source,
    run_id: str,
    state: Any,
    *,
    started_at: str,
    mode: str,
    resumed: bool = False,
    contact: str | None = None,
) -> dict[str, Any]:
    """Ingest a run's spooled records, schedule their refresh, and save the report."""
    report = ingest_records(
        engine,
        data_dir,
        source.key,
        store.spooled(run_id),
        state.errors.get(source.key, []),
        run_id=run_id,
        started_at=started_at,
        discovered=state.discovered.get(source.key, 0),
        on_offer=_schedule_hook(engine),
    )
    telemetry = state.telemetry.get(source.key) if state.telemetry else None
    report.update(
        {
            "mode": mode,
            "resumed": resumed,
            "user_agent": user_agent(contact),
            "crawl": {
                **(telemetry.as_dict() if telemetry else {}),
                "skipped": dict(state.skipped.get(source.key, Counter())),
                "complete_listings": sorted(state.complete_listings.get(source.key, ())),
            },
        }
    )
    write_report(data_dir, report)
    store.finish_run(run_id, report["status"], report)
    return report


def _duration(row: dict[str, Any]) -> str:
    start, end = instant(row.get("started_at")), instant(row.get("finished_at"))
    return f"{(end - start).total_seconds():.0f}s" if start and end else "-"


def show_runs(args: argparse.Namespace, engine: Database) -> int:
    """Recent discovery runs and refresh cycles from the catalogue's run log."""
    from django.apps import apps

    Run = apps.get_model("catalogue", "CollectionRun")
    if args.run_id:
        row = Run.objects.using(engine.alias).filter(run_id=args.run_id).values().first()
        path = args.data_dir / "reports" / f"{Path(args.run_id).name}.json"
        if path.is_file():
            print(path.read_text(encoding="utf-8"), end="")
            return 0
        if row is None:
            print(f"Run not found: {args.run_id}")
            return 1
        for key in ("error_summary", "completeness"):
            row[key] = json.loads(row[key] or "{}")
        print(json.dumps(row, ensure_ascii=False, indent=2, default=str))
        return 0
    if not 1 <= args.limit <= 1000:
        raise ValueError("--limit must be between 1 and 1000")
    query = Run.objects.using(engine.alias).order_by("-started_at")
    if args.source:
        query = query.filter(source_key=args.source)
    print("started (UTC)        kind           source     status    time  summary")
    for row in query.values()[: args.limit]:
        version = row["adapter_version"] or ""
        details = json.loads(row["completeness"] or "{}")
        if version == "refresh-cycle-v1":
            kind = "refresh-cycle"
            stretched = " stretched" if details.get("stretched") else ""
            summary = (
                f"due {details.get('due_at_start')}, checked {details.get('checked_offers')},"
                f" left {details.get('due_remaining')},"
                f" failures {sum((details.get('failures_by_reason') or {}).values())},"
                f" interval {details.get('effective_interval_hours')} h{stretched}"
            )
        elif version == "api-ingest-v1":
            kind = "api-ingest"
            summary = ", ".join(f"{k} {v}" for k, v in (details.get("counts") or {}).items())
        else:
            kind = "discovery"
            summary = (
                f"valid {row['records_valid']}, changed {row['records_upserted']},"
                f" same {row['records_deduplicated']}, rejected {row['records_rejected']}"
            )
        print(
            f"{row['started_at'][:19]:20} {kind:14} {row['source_key']:10} {row['status']:9}"
            f" {_duration(row):>5}  {summary}"
        )
        print(f"  run {row['run_id']}")
    return 0


def cycle_report_command(args: argparse.Namespace, engine: Database) -> int:
    """Live refresh-queue health per retailer plus its latest refresh cycle."""
    from .scheduler import queue_stats, recent_runs

    keys = [args.source] if args.source else [key for key in CRAWL_SOURCES if key in SOURCES]
    report = {"generated_at": utc_now().isoformat(), "sources": {}}
    for key in keys:
        stats = queue_stats(engine, key)
        if not stats["active_offers"] and not stats["unavailable"] and not args.source:
            continue
        cycles = recent_runs(engine, source_key=key, limit=5)
        latest = next((run for run in cycles if run["adapter_version"] == "refresh-cycle-v1"), None)
        report["sources"][key] = {
            "queue": stats,
            "latest_cycle": latest["completeness"] if latest else None,
            "latest_cycle_finished_at": latest["finished_at"] if latest else None,
        }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    print(
        "source     active   due  never  stale>1.5x  failing  pending-removal  unavailable"
        "  interval  last cycle"
    )
    for key, entry in report["sources"].items():
        queue, cycle = entry["queue"], entry["latest_cycle"] or {}
        interval = cycle.get("effective_interval_hours") or queue["interval_hours"]
        stretched = " (stretched)" if cycle.get("stretched") else ""
        print(
            f"{key:10} {queue['active_offers']:>6} {queue['due_now']:>5} {queue['never_checked']:>6}"
            f" {queue['stale_over_1_5x_interval']:>11} {queue['failing']:>8}"
            f" {queue['awaiting_removal_confirmation']:>16} {queue['unavailable']:>12}"
            f"  {interval:>5} h{stretched}  {(entry['latest_cycle_finished_at'] or '-')[:16]}"
        )
    return 0


def handle_scraper_command(
    args: argparse.Namespace, engine: Database, parser: argparse.ArgumentParser
) -> int | None:
    """Run `runs` or `cycle-report`; None for any other command."""
    try:
        if args.command == "runs":
            return show_runs(args, engine)
        if args.command == "cycle-report":
            return cycle_report_command(args, engine)
    except ValueError as exc:
        parser.error(str(exc))
    return None

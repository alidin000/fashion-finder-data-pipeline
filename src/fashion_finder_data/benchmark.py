"""Per-retailer extraction benchmark: how each shop is best collected, and what it costs.

It samples N representative product pages per retailer (stored offers when the
catalogue knows some, else pages found on the first category listing or sitemap),
fetches them through the same polite crawler as collection, and reports:

- the extraction method (feed/API, embedded JSON, JSON-LD, microdata, OpenGraph,
  HTML selectors, or "needs JavaScript"), with the success rate per page;
- response time p50/p95, the HTTP status mix and field coverage for price and sizes;
- whether JavaScript rendering looked necessary (client-rendered shell markers);
- the estimated refresh cycle time for the catalogue at the allowed request rate,
  and the concurrency a 12-hour cycle would need, or why no concurrency can reach
  it within robots.txt's Crawl-delay (the interval is stretched instead).
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from .collectors import MAX_LIMIT, PlanEntry, _spread, crawl
from .politeness import percentile
from .scheduler import HEADROOM
from .schema import utc_now
from .sources import DEFAULT_COLLECT_SOURCES, SOURCES, Source

METHOD_NAMES = {
    "jsonld": "JSON-LD",
    "microdata": "microdata",
    "opengraph": "OpenGraph meta",
    "state": "embedded JSON (state)",
    "embedded-json": "embedded JSON (category listing)",
    "html-selectors": "HTML selectors",
    "not modified": "not modified (304)",
}
TARGET_HOURS = 12.0


def _ms(value: float | None) -> int | None:
    return round(value * 1000) if value is not None else None


def classify(source: Source, pages: list[dict], stopped: str | None) -> dict[str, Any]:
    product_pages = [page for page in pages if page["kind"] == "product"]
    listing_pages = [page for page in pages if page["kind"] == "listing"]
    ok = [page for page in product_pages if page["records"]]
    methods = Counter(page["method"] for page in ok if page["method"])
    listing_methods = Counter(page["method"] for page in listing_pages if page["method"])
    js_suspects = [page for page in product_pages if not page["records"] and page["js_markers"]]
    if ok and not all(page["browser"] for page in ok):
        js = "no"
    elif js_suspects and len(js_suspects) == len(product_pages) - len(ok):
        js = "likely"
    else:
        js = "unknown"
    if source.status == "feed-only":
        method = "feed/API (partner feed required)"
    elif stopped:
        method = f"blocked: {stopped}"
    elif methods:
        method = METHOD_NAMES.get(methods.most_common(1)[0][0], methods.most_common(1)[0][0])
    elif js == "likely":
        method = "needs JavaScript"
    elif listing_methods:
        method = METHOD_NAMES.get(listing_methods.most_common(1)[0][0], "listing only")
    else:
        method = "no product data found"
    return {
        "method": method,
        "methods_seen": {METHOD_NAMES.get(key, key): value for key, value in methods.items()},
        "listing_method": (
            METHOD_NAMES.get(listing_methods.most_common(1)[0][0]) if listing_methods else None
        ),
        "product_pages": len(product_pages),
        "extraction_success": round(len(ok) / len(product_pages), 3) if product_pages else None,
        "price_coverage": round(sum(page["price"] for page in ok) / len(ok), 3) if ok else None,
        "size_coverage": round(sum(page["sizes"] for page in ok) / len(ok), 3) if ok else None,
        "javascript_needed": js,
        "errors": dict(Counter(page["error"] for page in product_pages if page["error"])),
    }


def estimate(
    source: Source,
    *,
    catalogue: int | None,
    spacing: float,
    latency: float | None,
) -> dict[str, Any]:
    """Cycle time at the allowed rate, and the concurrency a 12-hour cycle needs."""
    if not catalogue:
        return {"catalogue_size": catalogue, "note": "catalogue size unknown"}
    if source.refresh_strategy == "listing":
        listings = max(1, len(source.category_urls) or 1)
        units, per_unit, basis = listings, 2, f"{listings} curated category listings x 2 requests"
    else:
        units, per_unit, basis = catalogue, 1, f"{catalogue} product pages"
    requests = units * per_unit
    serial_hours = requests * spacing / 3600
    window = TARGET_HOURS * 3600 * HEADROOM
    # Requests start at most once per `spacing` per host, whatever the concurrency;
    # concurrency only helps when responses are slower than the spacing.
    limit_hours = serial_hours / HEADROOM
    if limit_hours > TARGET_HOURS:
        needed: int | str = "not reachable within the allowed request rate"
        effective = round(limit_hours, 1)
    else:
        per_request = max(latency or 1.0, 0.1)
        needed = max(1, math.ceil(requests * per_request / window))
        effective = TARGET_HOURS
    return {
        "catalogue_size": catalogue,
        "refresh_basis": basis,
        "refresh_requests": requests,
        "spacing_seconds": spacing,
        "cycle_hours_one_connection": round(max(serial_hours, requests * (latency or 0) / 3600), 2),
        "concurrency_needed_for_12h": needed,
        "effective_interval_hours": effective,
    }


def run_benchmark(args: Any, backend: Any) -> dict[str, Any]:
    started = utc_now()
    keys = list(dict.fromkeys(args.source)) if args.source else list(DEFAULT_COLLECT_SOURCES)
    sources = [SOURCES[key] for key in keys]
    if not 1 <= args.per_source <= 500:
        raise ValueError("--per-source must be between 1 and 500")
    crawlable = [source for source in sources if source.crawl_enabled]
    known = {source.key: backend.known(source.key) for source in crawlable}
    plans: dict[str, list[PlanEntry]] = {}
    for source in crawlable:
        urls = sorted(known[source.key])
        if urls:  # sample stored offers across the catalogue
            plans[source.key] = [
                PlanEntry(url, "product") for url in _spread(urls, args.per_source)
            ]
    state = None
    if crawlable:
        state = crawl(
            crawlable,
            MAX_LIMIT,
            mode="auto",
            plans=plans or None,
            max_requests=args.max_requests,
            delay=args.delay,
            contact=args.contact_url,
            benchmark_pages=args.per_source,
            time_budget_minutes=args.time_budget_minutes,
            replay_dir=args.replay_dir,
        )
    report: dict[str, Any] = {"per_source": args.per_source, "sources": {}}
    for source in sources:
        if not source.crawl_enabled or state is None:
            report["sources"][source.key] = {
                "method": "feed/API (partner feed required)"
                if source.status == "feed-only"
                else "not crawlable",
            }
            continue
        telemetry = state.telemetry.get(source.key) if state.telemetry else None
        crawl_info = telemetry.as_dict() if telemetry else {}
        pages = state.pages.get(source.key, [])
        latencies = [page["latency"] for page in pages if page["latency"] is not None]
        crawl_delay = max(crawl_info.get("crawl_delays", {}).values(), default=0.0)
        catalogue = len(known[source.key]) or state.listing_totals.get(source.key)
        report["sources"][source.key] = {
            **classify(source, pages, crawl_info.get("stopped")),
            "pages_fetched": len(pages),
            "requests": crawl_info.get("requests", 0),
            "status_counts": crawl_info.get("status_counts", {}),
            "latency_ms_p50": _ms(percentile(latencies, 0.5)),
            "latency_ms_p95": _ms(percentile(latencies, 0.95)),
            "crawl_delay_seconds": crawl_delay or None,
            "catalogue_basis": "stored offers" if known[source.key] else "listing or sitemap count",
            **estimate(
                source,
                catalogue=catalogue,
                spacing=max(args.delay, crawl_delay),
                latency=percentile(latencies, 0.5),
            ),
        }
    finished = utc_now()
    report |= {
        "run_id": f"benchmark-{started.strftime('%Y%m%dT%H%M%SZ')}",
        "backend": backend.name,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 1),
    }
    return report

"""Refresh and discovery runs, against the local catalogue or the ingest API.

The same command runs on a laptop, a Render cron job, a self-hosted runner or
GitHub Actions:

- **database mode** (default): due offers come from the local catalogue and
  results are applied to it directly;
- **API mode** (``--api-url`` or ``FASHION_FINDER_API_URL``, token in
  ``FASHION_FINDER_API_TOKEN``): due offers come from ``GET /api/v1/ingest/due``
  and results go to ``POST /api/v1/ingest/offers`` in batches of at most 500. The
  runner keeps nothing: its crawl state lives in a temporary directory that is
  deleted at exit.

Both modes apply results through ``scheduler.apply_batch`` (directly or behind the
API), so the refresh rules are identical everywhere.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

from .collectors import MAX_LIMIT, MAX_REQUESTS, CrawlState, PlanEntry, crawl
from .crawl_state import CrawlStore, product_key
from .discovery import without_page
from .schema import RawProductRecord, normalize, utc_now
from .sources import Source, user_agent

API_URL_ENV = "FASHION_FINDER_API_URL"
API_TOKEN_ENV = "FASHION_FINDER_API_TOKEN"
BATCH_ITEMS = 500
BATCH_BYTES = 3 * 1024 * 1024
POLICY_REQUESTS = 2  # robots.txt and tdmrep.json per host and run


class ApiError(RuntimeError):
    pass


# --------------------------------------------------------------------------- backends


class DatabaseBackend:
    """Read due offers from and apply results to the local Django catalogue."""

    name = "database"

    def __init__(self, engine: Any):
        self.engine = engine

    def due(self, source_key: str, limit: int) -> tuple[list[dict], dict]:
        from .scheduler import due_offers, queue_stats

        return due_offers(self.engine, source_key, limit=limit), queue_stats(
            self.engine, source_key
        )

    def known(self, source_key: str) -> set[str]:
        from .scheduler import known_urls

        urls: set[str] = set()
        cursor: int | None = 0
        while cursor is not None:
            page, cursor = known_urls(self.engine, source_key, after_id=cursor, limit=5000)
            urls.update(page)
        return urls

    def push(
        self,
        items: list[dict],
        *,
        intervals: dict[str, float],
        run_id: str,
        cycle_report: dict | None = None,
    ) -> list[dict]:
        from .scheduler import apply_batch, save_cycle_rows

        results: list[dict] = []
        for start in range(0, len(items), BATCH_ITEMS):
            chunk = items[start : start + BATCH_ITEMS]
            for result in apply_batch(self.engine, chunk, intervals=intervals):
                result["index"] += start
                results.append(result)
        if cycle_report:
            save_cycle_rows(self.engine, cycle_report)
        return results


class ApiBackend:
    """Talk to the Django ingest API with a bearer token; keep nothing locally."""

    name = "api"

    def __init__(self, base_url: str, token: str, *, contact: str | None = None):
        parts = urllib.parse.urlsplit(base_url)
        local = parts.hostname in {"127.0.0.1", "localhost"}
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            raise ValueError("the API URL must use https (http only for localhost)")
        if not token:
            raise ValueError(f"set {API_TOKEN_ENV} for API mode")
        self.base = base_url.rstrip("/")
        self.token = token
        self.agent = f"{user_agent(contact)} refresh-runner"

    def _call(self, method: str, path: str, *, params: dict | None = None, body: Any = None) -> Any:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body, default=str).encode() if body is not None else None
        for attempt in range(4):
            request = urllib.request.Request(url, data=data, method=method)
            request.add_header("Authorization", f"Bearer {self.token}")
            request.add_header("User-Agent", self.agent)
            if data is not None:
                request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 503) and attempt < 3:
                    wait = exc.headers.get("Retry-After", "")
                    time.sleep(min(60, int(wait)) if wait.isdigit() else 5 * (attempt + 1))
                    continue
                detail = exc.read()[:300].decode("utf-8", "replace")
                raise ApiError(f"{method} {path}: HTTP {exc.code} {detail}") from None
            except urllib.error.URLError as exc:
                if attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise ApiError(f"{method} {path}: {exc.reason}") from None
        raise ApiError(f"{method} {path}: gave up after retries")

    def due(self, source_key: str, limit: int) -> tuple[list[dict], dict]:
        answer = self._call(
            "GET", "/api/v1/ingest/due", params={"source": source_key, "limit": limit}
        )
        return answer["offers"], answer["stats"]

    def known(self, source_key: str) -> set[str]:
        urls: set[str] = set()
        after: int | None = 0
        while after is not None:
            answer = self._call(
                "GET",
                "/api/v1/ingest/known",
                params={"source": source_key, "after": after, "limit": 10000},
            )
            urls.update(answer["urls"])
            after = answer["next_after"]
        return urls

    def push(
        self,
        items: list[dict],
        *,
        intervals: dict[str, float],
        run_id: str,
        cycle_report: dict | None = None,
    ) -> list[dict]:
        results: list[dict] = []
        batches = list(_batches(items)) or [[]]
        offset = 0
        for number, batch in enumerate(batches):
            body = {"run_id": run_id, "intervals": intervals, "items": batch}
            if cycle_report and number == len(batches) - 1:
                body["cycle_report"] = cycle_report
            answer = self._call("POST", "/api/v1/ingest/offers", body=body)
            for result in answer["results"]:
                result["index"] += offset
                results.append(result)
            offset += len(batch)
        return results

    def photos(self, source_key: str, *, after: int, limit: int, per_offer: int) -> dict:
        """A page of offers whose photos may be indexed, with those already indexed."""
        return self._call(
            "GET",
            "/api/v1/ingest/photos",
            params={"source": source_key, "after": after, "limit": limit, "per_offer": per_offer},
        )

    def push_photo_vectors(
        self, feature_version: str, entries: list[dict], *, run_id: str
    ) -> list[dict]:
        """Upload at most 500 photo embeddings; per-entry ``indexed`` or ``rejected``."""
        answer = self._call(
            "POST",
            "/api/v1/ingest/image-index",
            body={"run_id": run_id, "feature_version": feature_version, "entries": entries},
        )
        return answer["results"]

    def touch(self) -> None:
        """Any request keeps a free Render instance awake (it sleeps after 15 idle minutes)."""
        self._call("GET", "/api/v1/health")


def _batches(items: list[dict]):
    batch: list[dict] = []
    size = 0
    for item in items:
        length = len(json.dumps(item, default=str))
        if batch and (len(batch) >= BATCH_ITEMS or size + length > BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += length
    if batch:
        yield batch


def backend_from(args: Any, engine_factory: Any) -> DatabaseBackend | ApiBackend:
    url = getattr(args, "api_url", None) or os.environ.get(API_URL_ENV)
    if url:
        return ApiBackend(url, os.environ.get(API_TOKEN_ENV, ""), contact=args.contact_url)
    return DatabaseBackend(engine_factory())


# --------------------------------------------------------------------------- items


def observation(record: RawProductRecord) -> dict[str, Any]:
    """An ingest item for an extracted record, without the bulky source payload."""
    data = record.model_dump(mode="json")
    data["product"].pop("_source_payload", None)
    return {"type": "observation", "record": data}


def refresh_items(
    state: CrawlState, due_by_url: dict[str, dict[str, list[dict]]]
) -> tuple[list[dict], Counter]:
    """Turn crawl outcomes into ingest items for the planned offers.

    Offers whose check was not attempted (time budget, request budget, a stopped
    source) produce no item and stay due.
    """
    items: list[dict] = []
    reasons: Counter = Counter()
    now = utc_now().isoformat()
    for records in state.records.values():
        items.extend(observation(record) for record in records)
    for source_key, groups in due_by_url.items():
        outcomes = state.outcomes.get(source_key, {})
        complete = state.complete_listings.get(source_key, set())
        seen_urls: set[str] = set()
        seen_skus: set[str] = set()
        for urls in state.listing_seen.get(source_key, {}).values():
            seen_urls |= urls
        for record in state.records.get(source_key, []):
            seen_urls.add(product_key(str(record.product.get("url"))))
            if record.product.get("sku"):
                seen_skus.add(str(record.product["sku"]))
        for url, offers in groups.items():
            outcome = outcomes.get(url)
            if outcome is None or outcome["outcome"] == "skipped":
                reasons["not attempted"] += len(offers)
                continue
            kind = offers[0]["check_kind"]
            for offer in offers:
                base = {"offer_id": offer["id"], "source_key": source_key, "checked_at": now}
                status = outcome["outcome"]
                if kind == "listing":
                    if status == "gone":
                        items.append({"type": "missing", **base, "listing_url": url})
                        reasons["listing gone"] += 1
                    elif status == "failed":
                        items.append(
                            {
                                "type": "failure",
                                **base,
                                "reason": outcome.get("reason") or "listing failed",
                            }
                        )
                        reasons[outcome.get("reason") or "listing failed"] += 1
                    elif without_page(url) in complete and not _seen(offer, seen_urls, seen_skus):
                        items.append({"type": "missing", **base, "listing_url": url})
                        reasons["missing from complete listing"] += 1
                    elif not _seen(offer, seen_urls, seen_skus):
                        reasons["listing not read completely"] += 1
                    continue
                if status == "not_modified":
                    items.append({"type": "not_modified", **base})
                elif status in {"gone", "failed"}:
                    reason = outcome.get("reason") or status
                    items.append(
                        {
                            "type": "failure",
                            **base,
                            "reason": reason[:200],
                            "gone": status == "gone",
                            "http_status": outcome.get("status"),
                        }
                    )
                    reasons[reason[:80]] += 1
                elif not _seen(offer, seen_urls, seen_skus):
                    items.append(
                        {"type": "failure", **base, "reason": "page did not show this offer"}
                    )
                    reasons["page did not show this offer"] += 1
    return items, reasons


def _seen(offer: dict, urls: set[str], skus: set[str]) -> bool:
    return product_key(offer["product_url"]) in urls or (
        bool(offer.get("sku")) and str(offer["sku"]) in skus
    )


# --------------------------------------------------------------------------- planning


def plan_refresh(
    offers: list[dict], *, requests_available: int
) -> tuple[list[PlanEntry], dict[str, list[dict]]]:
    """Group due offers by the URL that refreshes them, most overdue first, within budget."""
    groups: dict[str, list[dict]] = {}
    for offer in offers:
        groups.setdefault(offer["check_url"], []).append(offer)
    plan: list[PlanEntry] = []
    chosen: dict[str, list[dict]] = {}
    spent = 0
    for index, (url, members) in enumerate(groups.items()):
        cost = 2 if members[0]["check_kind"] == "listing" else 1  # LPP: page 1, then one jump
        if spent + cost > requests_available:
            break
        spent += cost
        plan.append(PlanEntry(url, members[0]["check_kind"], priority=-index, reason="due"))
        chosen[url] = members
    return plan, chosen


def _split(total: int, parts: int) -> int:
    return max(1, math.ceil(total / max(1, parts)))


# --------------------------------------------------------------------------- refresh


def run_refresh(args: Any, backend: DatabaseBackend | ApiBackend, sources: list[Source]) -> dict:
    """Refresh due offers of each source within the request and time budgets."""
    from .scheduler import plan_capacity

    started = utc_now()
    run_id = f"refresh-{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:6]}"
    per_source = _split(args.limit, len(sources))
    budget_minutes = args.time_budget_minutes
    plans: dict[str, list[PlanEntry]] = {}
    chosen: dict[str, dict[str, list[dict]]] = {}
    stats: dict[str, dict] = {}
    for source in sources:
        offers, stats[source.key] = backend.due(source.key, per_source)
        by_time = int(budget_minutes * 60 / max(args.delay, 1.0)) - POLICY_REQUESTS
        available = max(0, min(args.max_requests - POLICY_REQUESTS, by_time))
        plans[source.key], chosen[source.key] = plan_refresh(offers, requests_available=available)
    active = [source for source in sources if plans[source.key]]
    state = CrawlState()
    if active:
        with tempfile.TemporaryDirectory(prefix="fashion-finder-refresh-") as scratch:
            store = (
                CrawlStore(Path(scratch) / "crawl-state.sqlite")
                if isinstance(backend, ApiBackend)
                else CrawlStore(args.data_dir / "crawl-state.sqlite")
            )
            try:
                state = crawl(
                    active,
                    MAX_LIMIT,
                    mode="refresh",
                    plans={source.key: plans[source.key] for source in active},
                    max_requests=args.max_requests,
                    delay=args.delay,
                    contact=args.contact_url,
                    store=store,
                    time_budget_minutes=budget_minutes,
                    replay_dir=args.replay_dir,
                )
            finally:
                store.close()
    items, reasons = refresh_items(state, chosen)
    intervals: dict[str, float] = {}
    capacities: dict[str, dict] = {}
    for source in sources:
        telemetry = state.telemetry.get(source.key) if state.telemetry else None
        crawl_delay = max((telemetry.crawl_delays.values() if telemetry else []), default=0.0)
        listing = source.refresh_strategy == "listing"
        capacity = plan_capacity(
            source.key,
            offers=stats[source.key]["active_offers"],
            units=stats[source.key]["refresh_units"],
            spacing_seconds=max(args.delay, crawl_delay),
            requests_per_unit=2 if listing else 1,
        )
        capacities[source.key] = capacity.as_dict()
        intervals[source.key] = capacity.effective_interval_hours
    results = backend.push(items, intervals=intervals, run_id=run_id, cycle_report=None)
    report = cycle_report(
        run_id=run_id,
        started=started,
        sources=sources,
        stats=stats,
        chosen=chosen,
        state=state,
        reasons=reasons,
        capacities=capacities,
        results=results,
        items=items,
        backend=backend.name,
    )
    backend.push([], intervals=intervals, run_id=run_id, cycle_report=report)
    return report


def cycle_report(
    *,
    run_id: str,
    started: Any,
    sources: list[Source],
    stats: dict[str, dict],
    chosen: dict[str, dict[str, list[dict]]],
    state: CrawlState,
    reasons: Counter,
    capacities: dict[str, dict],
    results: list[dict],
    items: list[dict],
    backend: str,
) -> dict[str, Any]:
    by_source: dict[str, Counter] = {}
    details: dict[str, Counter] = {}
    for result in results:
        key = result.get("source_key") or "unknown"
        by_source.setdefault(key, Counter())[result["status"]] += 1
        if result.get("detail"):
            details.setdefault(key, Counter())[result["detail"]] += 1
    failures: dict[str, Counter] = {}
    for item in items:
        if item["type"] in {"failure", "missing"}:
            reason = item.get("reason") or "missing from complete listing"
            failures.setdefault(item["source_key"], Counter())[reason[:80]] += 1
    report_sources = {}
    for source in sources:
        key = source.key
        if not stats[key]["active_offers"] and not stats[key]["due_now"] and len(sources) > 1:
            continue  # nothing stored for this retailer yet
        telemetry = state.telemetry.get(key) if state.telemetry else None
        crawl_info = telemetry.as_dict() if telemetry else {}
        planned = sum(len(group) for group in chosen.get(key, {}).values())
        counts = by_source.get(key, Counter())
        attempted = planned - sum(
            1
            for url, group in chosen.get(key, {}).items()
            for _ in group
            if state.outcomes.get(key, {}).get(url, {}).get("outcome") in (None, "skipped")
        )
        due = stats[key]["due_now"]
        remaining = max(0, due - attempted)
        report_sources[key] = {
            "due_at_start": due,
            "active_offers": stats[key]["active_offers"],
            "stale_over_1_5x_interval": stats[key]["stale_over_1_5x_interval"],
            "planned_offers": planned,
            "checked_offers": attempted,
            "results": dict(counts),
            "failures_by_reason": dict(failures.get(key, Counter())),
            "removal": dict(details.get(key, Counter())),
            "due_remaining": remaining,
            "cycle_complete": remaining == 0,
            "requests": crawl_info.get("requests", 0),
            "status_counts": crawl_info.get("status_counts", {}),
            "latency_ms_p50": crawl_info.get("latency_ms_p50"),
            "latency_ms_p95": crawl_info.get("latency_ms_p95"),
            "stopped": crawl_info.get("stopped"),
            "budget_exhausted": crawl_info.get("budget_exhausted", False),
            "crawl_delays": crawl_info.get("crawl_delays", {}),
            "duration_seconds": crawl_info.get("duration_seconds"),
            **{
                key_: capacities.get(key, {}).get(key_)
                for key_ in ("interval_hours", "effective_interval_hours", "stretched", "reason")
            },
        }
    finished = utc_now()
    return {
        "run_id": run_id,
        "backend": backend,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 1),
        "not_attempted": reasons.get("not attempted", 0),
        "sources": report_sources,
    }


# --------------------------------------------------------------------------- discovery


def run_discovery_api(
    args: Any, backend: ApiBackend, sources: list[Source], overrides: dict | None
) -> dict:
    """Discover new offers with the crawler and send them to the ingest API."""
    from .pipeline import field_coverage

    started = utc_now()
    run_id = f"discover-{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:6]}"
    known = {source.key: backend.known(source.key) for source in sources}
    with tempfile.TemporaryDirectory(prefix="fashion-finder-discover-") as scratch:
        store = CrawlStore(Path(scratch) / "crawl-state.sqlite")
        try:
            state = crawl(
                sources,
                args.limit,
                overrides,
                browser=args.browser,
                browser_wait_ms=args.browser_wait_ms,
                mode=args.mode,
                max_requests=args.max_requests,
                delay=args.delay,
                contact=args.contact_url,
                store=store,
                all_categories=args.all_categories,
                max_sitemaps=args.max_sitemaps,
                known=known,
                time_budget_minutes=args.time_budget_minutes,
                replay_dir=args.replay_dir,
            )
        finally:
            store.close()
    items = [observation(record) for records in state.records.values() for record in records]
    results = backend.push(items, intervals={}, run_id=run_id)
    counts: dict[str, Counter] = {}
    for result in results:
        counts.setdefault(result.get("source_key") or "unknown", Counter())[result["status"]] += 1
    report_sources = {}
    for source in sources:
        offers = []
        for record in state.records.get(source.key, []):
            try:
                offers.append(normalize(record))
            except (ValueError, TypeError):
                continue
        telemetry = state.telemetry.get(source.key) if state.telemetry else None
        report_sources[source.key] = {
            "known_before": len(known[source.key]),
            "records": len(state.records.get(source.key, [])),
            "results": dict(counts.get(source.key, Counter())),
            "field_coverage": field_coverage(offers),
            "errors": dict(Counter(state.errors.get(source.key, [])).most_common(10)),
            "crawl": telemetry.as_dict() if telemetry else {},
        }
    return {
        "run_id": run_id,
        "backend": "api",
        "started_at": started.isoformat(),
        "finished_at": utc_now().isoformat(),
        "sources": report_sources,
    }


# --------------------------------------------------------------------------- output


def markdown(report: dict[str, Any], title: str) -> str:
    """A short Markdown summary for ``$GITHUB_STEP_SUMMARY`` or a terminal."""
    lines = [
        f"## {title}",
        "",
        (
            f"Run `{report['run_id']}` ({report.get('backend')}),"
            f" {report['started_at'][:16]}Z, {report.get('duration_seconds', '-')} s."
        ),
        "",
    ]
    rows = report["sources"]
    if not rows:
        return "\n".join([*lines, "No sources selected."]) + "\n"
    first = next(iter(rows.values()))
    if "method" in first:
        lines += [
            (
                "| Source | Method | Success | Price | Sizes | p50/p95 ms | HTTP | JS | Catalogue"
                " | Cycle h (1 conn.) | Concurrency for 12 h |"
            ),
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for key, row in rows.items():
            statuses = ", ".join(
                f"{code}: {count}" for code, count in row.get("status_counts", {}).items()
            )
            lines.append(
                f"| {key} | {row['method']} | {row.get('extraction_success')} |"
                f" {row.get('price_coverage')} | {row.get('size_coverage')} |"
                f" {row.get('latency_ms_p50')}/{row.get('latency_ms_p95')} | {statuses} |"
                f" {row.get('javascript_needed', '-')} | {row.get('catalogue_size', '-')} |"
                f" {row.get('cycle_hours_one_connection', '-')} |"
                f" {row.get('concurrency_needed_for_12h', '-')} |"
            )
        return "\n".join(lines) + "\n"
    if "due_at_start" in first:
        lines += [
            (
                "| Source | Due | Checked | Updated/unchanged/new | Failures | Left due | Requests"
                " | p50/p95 ms | Interval | Stopped |"
            ),
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for key, row in rows.items():
            obs = row["results"]
            interval = f"{row.get('effective_interval_hours') or '-'} h"
            if row.get("stretched"):
                interval += " (stretched)"
            lines.append(
                f"| {key} | {row['due_at_start']} | {row['checked_offers']} |"
                f" {obs.get('updated', 0)}/{obs.get('unchanged', 0)}/{obs.get('created', 0)} |"
                f" {sum(row['failures_by_reason'].values())} | {row['due_remaining']} |"
                f" {row['requests']} | {row['latency_ms_p50'] or '-'}/{row['latency_ms_p95'] or '-'} |"
                f" {interval} | {row['stopped'] or ''} |"
            )
        for key, row in rows.items():
            if row["failures_by_reason"]:
                reasons = ", ".join(
                    f"{reason}: {count}" for reason, count in row["failures_by_reason"].items()
                )
                lines.append(f"\n- **{key}** failures: {reasons}")
            if row.get("stretched"):
                lines.append(f"\n- **{key}** interval stretched: {row.get('reason')}")
            if row["stale_over_1_5x_interval"]:
                lines.append(
                    f"\n- **{key}**: {row['stale_over_1_5x_interval']} offers older than 1.5x the interval"
                )
    else:
        lines += ["| Source | Records | Results | Requests | Stopped |", "|---|---|---|---|---|"]
        for key, row in rows.items():
            results = ", ".join(f"{name} {count}" for name, count in row["results"].items())
            lines.append(
                f"| {key} | {row['records']} | {results or '-'} |"
                f" {row['crawl'].get('requests', 0)} | {row['crawl'].get('stopped') or ''} |"
            )
    return "\n".join(lines) + "\n"


def publish(report: dict[str, Any], title: str, render: Any = None) -> None:
    """Print the JSON report; also append Markdown to the GitHub job summary if present.

    ``render`` turns the report into Markdown (default: :func:`markdown`).
    """
    print(json.dumps(report, ensure_ascii=False, default=str))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write((render or markdown)(report, title))
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for key, row in report["sources"].items():
            stopped = row.get("stopped") or row.get("crawl", {}).get("stopped")
            if stopped:
                print(f"::warning title=Source {key} stopped::{stopped}")


def refresh_request_budget(limit: int, sources: int) -> int:
    return min(MAX_REQUESTS, _split(limit, sources) * 2 + 10)

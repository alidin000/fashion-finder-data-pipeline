"""Authenticated ingest API for refresh runners (GitHub Actions, Render cron, laptops).

Routes (all need ``Authorization: Bearer <FASHION_FINDER_INGEST_TOKEN>``):
- ``GET  /api/v1/ingest/due?source=&limit=``: offers due for refresh;
- ``GET  /api/v1/ingest/known?source=&after=&limit=``: catalogue URLs, so discovery
  only fetches new products;
- ``POST /api/v1/ingest/offers``: a batch of at most 500 refreshed, newly discovered
  or failed offers, applied idempotently with per-item results.

Without the environment variable the API answers 503 for every call; it never falls
back to open access. Ingest only stores offers: source policies still decide what
shoppers see. CSRF does not apply because requests carry no cookies or session and
are authenticated by the bearer token alone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections import Counter, deque
from datetime import UTC, datetime

from django.db import DatabaseError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from pydantic import ValidationError

from fashion_finder_data.api.views import _config
from fashion_finder_data.scheduler import (
    MAX_BATCH_ITEMS,
    BatchEnvelope,
    apply_batch,
    due_offers,
    known_urls,
    queue_stats,
    save_cycle_rows,
    save_run_row,
    summarise,
)
from fashion_finder_data.sources import NON_RETAIL_SOURCES, SOURCES

TOKEN_ENV = "FASHION_FINDER_INGEST_TOKEN"
MAX_BODY_BYTES = 4 * 1024 * 1024
MIN_TOKEN_LENGTH = 32
# Per-process sliding-window limits; a runner needs far fewer calls than this.
RATE_LIMITS = {"GET": (120, 60.0), "POST": (60, 60.0)}
_calls: dict[tuple[str, str], deque] = {}
_calls_lock = threading.Lock()
audit_log = logging.getLogger("fashion_finder_data.ingest.audit")


def _error(detail: str, status: int, **headers: str) -> JsonResponse:
    response = JsonResponse({"detail": detail}, status=status)
    for key, value in headers.items():
        response[key] = value
    return response


def _configured_token() -> str | None:
    token = os.environ.get(TOKEN_ENV, "")
    return token if len(token) >= MIN_TOKEN_LENGTH else None


def _actor(token: str) -> str:
    return "ingest-token:" + hashlib.sha256(token.encode()).hexdigest()[:10]


def _authenticate(request) -> tuple[str | None, JsonResponse | None]:
    expected = _configured_token()
    if expected is None:
        return None, _error("the ingest API is not configured", 503)
    header = request.headers.get("Authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        supplied.strip().encode(), expected.encode()
    ):
        return None, _error(
            "missing or invalid ingest token", 401, **{"WWW-Authenticate": "Bearer"}
        )
    return _actor(expected), None


def _throttled(actor: str, method: str) -> JsonResponse | None:
    limit, window = RATE_LIMITS[method]
    now = time.monotonic()
    with _calls_lock:
        calls = _calls.setdefault((actor, method), deque())
        while calls and now - calls[0] > window:
            calls.popleft()
        if len(calls) >= limit:
            wait = max(1, int(window - (now - calls[0])) + 1)
            return _error("too many ingest requests", 429, **{"Retry-After": str(wait)})
        calls.append(now)
    return None


def _guard(request, method: str) -> tuple[str | None, JsonResponse | None]:
    if request.method != method:
        return None, _error("method not allowed", 405)
    actor, problem = _authenticate(request)
    if problem is not None:
        return None, problem
    return actor, _throttled(actor, method)


def _source(request) -> tuple[str | None, JsonResponse | None]:
    source = request.GET.get("source")
    if source not in SOURCES or source in NON_RETAIL_SOURCES:
        return None, _error("source must name a configured retailer", 400)
    return source, None


def _limit(request, default: int, maximum: int) -> int | None:
    try:
        value = int(request.GET.get("limit", default))
    except ValueError:
        return None
    return value if 1 <= value <= maximum else None


@csrf_exempt
def due(request):
    _actor_name, problem = _guard(request, "GET")
    if problem is not None:
        return problem
    source, problem = _source(request)
    if problem is not None:
        return problem
    limit = _limit(request, 500, 5000)
    if limit is None:
        return _error("limit must be between 1 and 5000", 400)
    database, _ = _config()
    try:
        offers = due_offers(database, source, limit=limit)
        stats = queue_stats(database, source)
    except DatabaseError:
        return _error("the catalogue is busy; try again shortly", 503)
    return JsonResponse({"source": source, "stats": stats, "offers": offers})


@csrf_exempt
def known(request):
    _actor_name, problem = _guard(request, "GET")
    if problem is not None:
        return problem
    source, problem = _source(request)
    if problem is not None:
        return problem
    limit = _limit(request, 5000, 10000)
    try:
        after = int(request.GET.get("after", 0))
    except ValueError:
        after = -1
    if limit is None or after < 0:
        return _error("limit must be 1–10000 and after a nonnegative id", 400)
    database, _ = _config()
    try:
        urls, cursor = known_urls(database, source, after_id=after, limit=limit)
    except DatabaseError:
        return _error("the catalogue is busy; try again shortly", 503)
    return JsonResponse({"source": source, "urls": urls, "next_after": cursor})


@csrf_exempt
def offers(request):
    actor, problem = _guard(request, "POST")
    if problem is not None:
        return problem
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY_BYTES:
        return _error(f"request body exceeds {MAX_BODY_BYTES} bytes", 413)
    body = request.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        return _error(f"request body exceeds {MAX_BODY_BYTES} bytes", 413)
    if request.content_type != "application/json":
        return _error("content type must be application/json", 415)
    try:
        envelope = BatchEnvelope.model_validate(json.loads(body))
    except (ValueError, ValidationError) as exc:
        too_many = isinstance(exc, ValidationError) and any(
            error.get("type") == "too_long" for error in exc.errors()
        )
        if too_many:
            return _error(f"a batch holds at most {MAX_BATCH_ITEMS} items", 413)
        return _error("body must be a JSON batch with an items list", 400)
    database, _ = _config()
    started = datetime.now(UTC)
    try:
        results = apply_batch(database, envelope.items, intervals=envelope.intervals)
        summary = summarise(results, envelope.items)
        for source_key, entry in summary.items():
            if source_key not in SOURCES:
                continue
            save_run_row(
                database,
                source_key=source_key,
                kind="api-ingest",
                started_at=started,
                counts=entry["counts"],
                errors={**entry["rejection_reasons"], **entry["failures"]},
                details={
                    "actor": actor,
                    "client_run_id": envelope.run_id,
                    "runner": request.headers.get("User-Agent", "")[:120],
                    "counts": dict(entry["counts"]),
                    "failures_by_reason": dict(entry["failures"]),
                },
            )
        if envelope.cycle_report:
            save_cycle_rows(database, envelope.cycle_report, actor=actor)
    except DatabaseError:
        return _error("the catalogue is busy; try again shortly", 503)
    counts = Counter(result["status"] for result in results)
    audit_log.info(
        "ingest batch actor=%s run=%s items=%d results=%s",
        actor,
        envelope.run_id,
        len(results),
        json.dumps(dict(counts), sort_keys=True),
    )
    return JsonResponse({"run_id": envelope.run_id, "counts": dict(counts), "results": results})

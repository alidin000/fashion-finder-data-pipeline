"""Due-queue refresh scheduling on the catalogue database.

Discovery finds offers; refresh re-checks known offers when ``next_refresh_at`` is
due. This module owns the rules, and both the local database backend and the
authenticated ingest API apply results through ``apply_batch``, so a runner on
GitHub Actions, a Render cron job or a laptop behaves the same.

Rules:
- success: update the offer if a shopper-visible field changed (full upsert and a
  price observation when the price changed); otherwise write only the check time
  and the next due time; then reschedule one interval later;
- failure: exponential backoff from 30 minutes, capped at 24 hours;
- HTTP 404/410, a redirect to a non-product page, or a discontinued product:
  recheck after 6 hours and mark the offer ``unavailable`` only after two such
  answers at least 6 hours apart; unavailable offers are rechecked weekly and come
  back as soon as a check finds them again;
- an offer missing from a complete category listing is rechecked on its own
  product page 6 hours later, never marked unavailable from the listing alone.

Queries are batched (``iterator``/slices) and use only portable ORM expressions,
so the same code runs on SQLite and PostgreSQL. A single refresh worker needs no
row locks; parallel workers would select due rows with
``select_for_update(skip_locked=True)``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import uuid4

from django.apps import apps
from django.db import transaction
from django.db.models import F, Min, Q
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

from .discovery import without_page
from .schema import RawProductRecord, RetailerOffer, canonical_url, normalize, utc_now
from .sources import NON_RETAIL_SOURCES, SOURCES, SourceKey, allowed_url
from .storage import KEEP_WHEN_MISSING, Database, _money_text, upsert_offer

GONE_CONFIRMATIONS = 2
GONE_MIN_SPAN = timedelta(hours=6)
CONFIRM_AFTER = timedelta(hours=6)
UNAVAILABLE_RECHECK = timedelta(days=7)
BACKOFF_BASE = timedelta(minutes=30)
BACKOFF_MAX = timedelta(hours=24)
HEADROOM = 0.8  # plan cycles to use at most 80 % of the window; the rest absorbs retries
MAX_INTERVAL_HOURS = 24 * 14
STALE_FACTOR = 1.5
CHUNK = 500
MAX_BATCH_ITEMS = 500
FUTURE_TOLERANCE = timedelta(minutes=5)
MISSING_FROM_LISTING = "missing from complete listing"
SHOPPER_FIELDS = (
    "source_product_id",
    "source_url",
    "canonical_url",
    "title",
    "brand",
    "category",
    "source_category",
    "description",
    "price_amount",
    "currency",
    "original_price_amount",
    "image_urls",
    "colour_names",
    "size_options",
    "availability_status",
)


def _models():
    return (
        apps.get_model("catalogue", "Retailer"),
        apps.get_model("catalogue", "RetailerOffer"),
        apps.get_model("catalogue", "CollectionRun"),
    )


def backoff(failures: int) -> timedelta:
    """30 min, 1 h, 2 h, ... capped at 24 h, for the n-th consecutive failure."""
    return min(BACKOFF_BASE * (2 ** max(0, failures - 1)), BACKOFF_MAX)


def stagger(url: str) -> float:
    """A stable fraction in [0.5, 1) that spreads first refreshes across the window."""
    digest = hashlib.sha256(url.encode()).digest()
    return 0.5 + int.from_bytes(digest[:4], "big") / 2**33


def configured_interval(source_key: str) -> float:
    source = SOURCES.get(source_key)
    return source.interval_hours if source else 12.0


def effective_interval(source_key: str, reported: float | None = None) -> float:
    """The configured interval, stretched (never shortened) by a runner's capacity report."""
    base = configured_interval(source_key)
    if reported is None or not math.isfinite(reported):
        return base
    return min(MAX_INTERVAL_HOURS, max(base, float(reported)))


# --------------------------------------------------------------------------- capacity


@dataclass(frozen=True)
class Capacity:
    """Whether a retailer's refresh fits its interval within its access limits."""

    source_key: str
    offers: int
    units: int
    requests_per_unit: float
    spacing_seconds: float
    interval_hours: float
    effective_interval_hours: float
    cycle_hours: float
    stretched: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def plan_capacity(
    source_key: str,
    *,
    offers: int,
    units: int,
    spacing_seconds: float,
    requests_per_unit: float = 1.0,
) -> Capacity:
    """Stretch the interval when the access limits cannot refresh everything in time.

    ``spacing_seconds`` is the gap the crawler must keep between requests to the
    host: the larger of the configured delay and the robots.txt Crawl-delay. The
    scheduler never shortens it to meet a target.
    """
    interval = configured_interval(source_key)
    cycle_seconds = units * requests_per_unit * spacing_seconds
    needed_hours = cycle_seconds / HEADROOM / 3600
    effective = min(MAX_INTERVAL_HOURS, max(interval, needed_hours))
    stretched = effective > interval
    reason = (
        f"{units} refresh requests x {requests_per_unit:g} at {spacing_seconds:g} s spacing need"
        f" {cycle_seconds / 3600:.1f} h (+{(1 - HEADROOM) * 100:.0f} % headroom); interval"
        f" stretched from {interval:g} h to {effective:.1f} h"
        if stretched
        else f"fits: {cycle_seconds / 3600:.1f} h of requests per {interval:g} h interval"
    )
    return Capacity(
        source_key,
        offers,
        units,
        requests_per_unit,
        spacing_seconds,
        interval,
        round(effective, 2),
        round(cycle_seconds / 3600, 2),
        stretched,
        reason,
    )


# --------------------------------------------------------------------------- queue


def _offers(engine: Database, source_key: str):
    _, Offer, _ = _models()
    return Offer.objects.using(engine.alias).filter(
        retailer__source_key=source_key, retailer__market_code="HU"
    )


def queue_stats(engine: Database, source_key: str, now: datetime | None = None) -> dict[str, Any]:
    """Counts that describe the refresh queue of one retailer (no rows loaded)."""
    now = now or utc_now()
    interval = timedelta(hours=configured_interval(source_key))
    offers = _offers(engine, source_key)
    active = offers.filter(status="active")
    stale_cutoff = now - interval * STALE_FACTOR
    due = Q(next_refresh_at__lte=now) | Q(next_refresh_at__isnull=True)
    source = SOURCES.get(source_key)
    total = active.count()
    if source is not None and source.refresh_strategy == "listing":
        listed = active.exclude(refresh_url__isnull=True)
        units = listed.values("refresh_url").distinct().count() + (total - listed.count())
    else:
        units = total
    oldest = active.aggregate(oldest=Min("last_checked_at"))["oldest"]
    return {
        "source_key": source_key,
        "interval_hours": interval.total_seconds() / 3600,
        "active_offers": total,
        "refresh_units": units,
        "due_now": active.filter(due).count(),
        "never_checked": active.filter(last_checked_at__isnull=True).count(),
        "stale_over_1_5x_interval": active.filter(
            Q(last_checked_at__lt=stale_cutoff)
            | Q(last_checked_at__isnull=True, last_seen_at__lt=stale_cutoff.isoformat())
        ).count(),
        "failing": active.filter(failure_count__gt=0).count(),
        "awaiting_removal_confirmation": active.filter(gone_checks__gt=0).count(),
        "unavailable": offers.filter(status="unavailable").count(),
        "oldest_check": oldest.isoformat() if oldest else None,
    }


def check_target(row: dict[str, Any], source_key: str) -> tuple[str, str]:
    """The URL a refresh fetches for an offer, and whether it is a listing or a product."""
    product = row["source_url"]
    listing = row.get("refresh_url")
    if (
        not listing
        or (row.get("last_error") or "").startswith(MISSING_FROM_LISTING)
        or row.get("gone_checks")
        or (row.get("status") == "unavailable")
    ):
        return product, "product"
    source = SOURCES.get(source_key)
    if source is None or not allowed_url(source, listing):
        return product, "product"
    return listing, "listing"


DUE_FIELDS = (
    "id",
    "source_url",
    "canonical_url",
    "refresh_url",
    "source_product_id",
    "price_amount",
    "currency",
    "size_options",
    "availability_status",
    "status",
    "next_refresh_at",
    "last_checked_at",
    "failure_count",
    "gone_checks",
    "last_error",
)


def due_offers(
    engine: Database, source_key: str, *, limit: int, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Up to ``limit`` due offers, most overdue (or never scheduled) first."""
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    now = now or utc_now()
    query = (
        _offers(engine, source_key)
        .filter(
            Q(status="active", next_refresh_at__lte=now)
            | Q(status="active", next_refresh_at__isnull=True)
            | Q(status="unavailable", next_refresh_at__lte=now)
        )
        .order_by(F("next_refresh_at").asc(nulls_first=True), "id")
        .values(*DUE_FIELDS)[:limit]
    )
    result = []
    for row in query.iterator(chunk_size=CHUNK):
        url, kind = check_target(row, source_key)
        result.append(
            {
                "id": row["id"],
                "source_key": source_key,
                "product_url": row["source_url"],
                "check_url": url,
                "check_kind": kind,
                "sku": row["source_product_id"],
                "price_amount": row["price_amount"],
                "currency": row["currency"],
                "size_options": json.loads(row["size_options"] or "[]"),
                "availability_status": row["availability_status"],
                "status": row["status"],
                "next_refresh_at": _iso(row["next_refresh_at"]),
                "last_checked_at": _iso(row["last_checked_at"]),
                "failure_count": row["failure_count"],
                "last_error": row["last_error"],
            }
        )
    return result


def known_urls(
    engine: Database, source_key: str, *, after_id: int = 0, limit: int = 5000
) -> tuple[list[str], int | None]:
    """A page of canonical URLs already in the catalogue, for discovery to skip."""
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    rows = list(
        _offers(engine, source_key)
        .filter(id__gt=after_id)
        .order_by("id")
        .values_list("id", "canonical_url")[:limit]
    )
    cursor = rows[-1][0] if len(rows) == limit else None
    return [url for _, url in rows], cursor


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


# --------------------------------------------------------------------------- items


class _Item(BaseModel):
    model_config = {"extra": "forbid"}


class ObservationItem(_Item):
    """A refreshed or newly discovered offer, as extracted (``RawProductRecord``)."""

    type: Literal["observation"]
    record: RawProductRecord
    offer_id: int | None = None


class NotModifiedItem(_Item):
    """HTTP 304 for an offer's product page: nothing changed since the last check."""

    type: Literal["not_modified"]
    offer_id: int
    source_key: SourceKey
    checked_at: datetime


class FailureItem(_Item):
    """A failed check; ``gone`` marks HTTP 404/410, a redirect away or a discontinued product."""

    type: Literal["failure"]
    offer_id: int
    source_key: SourceKey
    checked_at: datetime
    reason: str = Field(min_length=1, max_length=200)
    gone: bool = False
    http_status: int | None = Field(default=None, ge=100, le=599)


class MissingItem(_Item):
    """A complete read of the offer's category listing did not show it."""

    type: Literal["missing"]
    offer_id: int
    source_key: SourceKey
    checked_at: datetime
    listing_url: str = Field(max_length=2000)


IngestItem = Annotated[
    ObservationItem | NotModifiedItem | FailureItem | MissingItem, Field(discriminator="type")
]
ITEM_ADAPTER = TypeAdapter(IngestItem)


class BatchEnvelope(BaseModel):
    model_config = {"extra": "forbid"}

    run_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    intervals: dict[SourceKey, float] = Field(default_factory=dict)
    items: list[dict[str, Any]] = Field(max_length=MAX_BATCH_ITEMS)
    cycle_report: dict[str, Any] | None = None

    @field_validator("intervals")
    @classmethod
    def finite(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not math.isfinite(hours) or hours <= 0 for hours in value.values()):
            raise ValueError("intervals must be positive hours")
        return value


def _reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ())) or "item"
        return f"{where}: {first.get('msg', 'invalid')}"[:160]
    return (str(exc).split("\n", 1)[0] or type(exc).__name__)[:160]


def _aware(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)


def _would_change(existing: Any, offer: RetailerOffer) -> bool:
    """Mirror ``storage.upsert_offer``'s merge rules to see if an upsert would change a field."""
    values = {
        "source_product_id": offer.source_product_id or existing.source_product_id,
        "source_url": offer.source_url,
        "canonical_url": offer.canonical_url,
        "title": offer.title,
        "brand": offer.brand,
        "category": offer.category,
        "source_category": offer.source_category,
        "description": offer.description,
        "price_amount": _money_text(offer.price_amount),
        "currency": offer.currency,
        "original_price_amount": _money_text(offer.original_price_amount),
        "image_urls": json.dumps(offer.image_urls, ensure_ascii=False),
        "colour_names": json.dumps(offer.colour_names, ensure_ascii=False),
        "size_options": json.dumps(offer.size_options, ensure_ascii=False),
        "availability_status": offer.availability_status,
    }
    if offer.price_amount is None:
        for key in ("price_amount", "currency", "original_price_amount"):
            values[key] = getattr(existing, key)
    if offer.source_category is None:
        for key in ("category", "source_category"):
            values[key] = getattr(existing, key)
    for key in KEEP_WHEN_MISSING:
        if values[key] in (None, "[]"):
            values[key] = getattr(existing, key)
    if offer.availability_status == "unknown":
        values["availability_status"] = existing.availability_status
    if existing.status != "active":
        return True
    for key in SHOPPER_FIELDS:
        old, new = getattr(existing, key), values[key]
        if key in {"price_amount", "original_price_amount"} and old is not None and new is not None:
            if Decimal(old) != Decimal(new):
                return True
        elif old != new:
            return True
    return False


def _find(Offer: Any, engine: Database, offer: RetailerOffer) -> Any:
    query = Offer.objects.using(engine.alias).filter(
        retailer__source_key=offer.source_key, retailer__market_code=offer.market_code
    )
    if offer.source_product_id:
        match = query.filter(source_product_id=offer.source_product_id).first()
        if match is not None:
            return match
    return query.filter(canonical_url=offer.canonical_url).first()


def _listing_refresh_url(record: RawProductRecord, offer: RetailerOffer) -> str | None:
    source = SOURCES[record.source_key]
    if source.refresh_strategy != "listing":
        return None
    page = canonical_url(record.source_url)
    if page == offer.canonical_url:
        return None
    root = without_page(record.source_url)
    return root if allowed_url(source, root) else None


def _success_fields(
    checked: datetime, interval_hours: float, *, first_url: str | None = None
) -> dict[str, Any]:
    interval = timedelta(hours=interval_hours)
    due = checked + (interval * stagger(first_url) if first_url else interval)
    return {
        "last_checked_at": checked,
        "next_refresh_at": due,
        "failure_count": 0,
        "gone_checks": 0,
        "missing_since": None,
        "last_error": None,
    }


def apply_observation(
    engine: Database, item: ObservationItem, *, interval_hours: float, now: datetime
) -> dict[str, Any]:
    _, Offer, _ = _models()
    record = item.record
    checked = _aware(record.fetched_at)
    if checked > now + FUTURE_TOLERANCE:
        raise ValueError("observation time is in the future")
    if record.product.get("discontinued"):
        raise ValueError("a discontinued product must be sent as a 'gone' failure")
    offer = normalize(record)
    if offer.price_amount is not None and offer.price_amount <= 0:
        raise ValueError("price must be greater than zero")
    listing_url = _listing_refresh_url(record, offer)
    with transaction.atomic(using=engine.alias):
        existing = _find(Offer, engine, offer)
        if item.offer_id is not None and existing is not None and existing.pk != item.offer_id:
            raise ValueError("offer_id does not match the observed product")
        if existing is not None and existing.source_fetched_at:
            stored = datetime.fromisoformat(existing.source_fetched_at)
            if _aware(stored) > checked:
                upsert_offer(engine, offer)  # an older observation only fills in history
                return {"status": "unchanged", "offer_id": existing.pk, "detail": "late"}
        if existing is not None and not _would_change(existing, offer):
            fields = _success_fields(checked, interval_hours)
            fields |= {
                "source_fetched_at": checked.isoformat(),
                "last_seen_at": checked.isoformat(),
            }
            if listing_url:
                fields["refresh_url"] = listing_url
            elif (existing.last_error or "").startswith(MISSING_FROM_LISTING):
                fields["refresh_url"] = None  # it left that listing; check its page next time
            Offer.objects.using(engine.alias).filter(pk=existing.pk).update(**fields)
            return {"status": "unchanged", "offer_id": existing.pk}
        upsert_offer(engine, offer)
        stored_offer = _find(Offer, engine, offer)
        fields = _success_fields(
            checked, interval_hours, first_url=None if existing else offer.canonical_url
        )
        if listing_url:
            fields["refresh_url"] = listing_url
        elif existing is not None and (existing.last_error or "").startswith(MISSING_FROM_LISTING):
            fields["refresh_url"] = None
        Offer.objects.using(engine.alias).filter(pk=stored_offer.pk).update(**fields)
        return {"status": "updated" if existing else "created", "offer_id": stored_offer.pk}


def _known(engine: Database, offer_id: int, source_key: str) -> Any:
    _, Offer, _ = _models()
    offer = (
        Offer.objects.using(engine.alias)
        .filter(pk=offer_id, retailer__source_key=source_key)
        .first()
    )
    if offer is None:
        raise ValueError("unknown offer for this source")
    return offer


def apply_not_modified(
    engine: Database, item: NotModifiedItem, *, interval_hours: float, now: datetime
) -> dict[str, Any]:
    _, Offer, _ = _models()
    checked = _aware(item.checked_at)
    if checked > now + FUTURE_TOLERANCE:
        raise ValueError("check time is in the future")
    offer = _known(engine, item.offer_id, item.source_key)
    fields = _success_fields(checked, interval_hours) | {
        "source_fetched_at": checked.isoformat(),
        "last_seen_at": checked.isoformat(),
    }
    if offer.status != "active":
        raise ValueError("HTTP 304 cannot revive an unavailable offer; send an observation")
    Offer.objects.using(engine.alias).filter(pk=offer.pk).update(**fields)
    return {"status": "unchanged", "offer_id": offer.pk}


def apply_failure(engine: Database, item: FailureItem, *, now: datetime) -> dict[str, Any]:
    _, Offer, _ = _models()
    checked = _aware(item.checked_at)
    if checked > now + FUTURE_TOLERANCE:
        raise ValueError("check time is in the future")
    offer = _known(engine, item.offer_id, item.source_key)
    failures = offer.failure_count + 1
    fields: dict[str, Any] = {
        "last_checked_at": checked,
        "failure_count": failures,
        "last_error": item.reason[:255],
    }
    detail = "backoff"
    if item.gone:
        since = offer.missing_since or checked
        gone_checks = offer.gone_checks + 1
        fields |= {"gone_checks": gone_checks, "missing_since": since}
        if gone_checks >= GONE_CONFIRMATIONS and checked - _aware(since) >= GONE_MIN_SPAN:
            fields |= {
                "status": "unavailable",
                "next_refresh_at": checked + UNAVAILABLE_RECHECK,
                "last_error": f"unavailable: {item.reason}"[:255],
            }
            detail = "marked unavailable"
        else:
            fields["next_refresh_at"] = checked + CONFIRM_AFTER
            detail = "removal pending confirmation"
    else:
        fields["next_refresh_at"] = checked + backoff(failures)
    Offer.objects.using(engine.alias).filter(pk=offer.pk).update(**fields)
    return {"status": "recorded", "offer_id": offer.pk, "detail": detail}


def apply_missing(engine: Database, item: MissingItem, *, now: datetime) -> dict[str, Any]:
    _, Offer, _ = _models()
    checked = _aware(item.checked_at)
    if checked > now + FUTURE_TOLERANCE:
        raise ValueError("check time is in the future")
    offer = _known(engine, item.offer_id, item.source_key)
    Offer.objects.using(engine.alias).filter(pk=offer.pk).update(
        last_checked_at=checked,
        failure_count=offer.failure_count + 1,
        last_error=MISSING_FROM_LISTING,
        missing_since=offer.missing_since or checked,
        next_refresh_at=checked + CONFIRM_AFTER,
    )
    return {"status": "recorded", "offer_id": offer.pk, "detail": "product page check scheduled"}


def apply_batch(
    engine: Database,
    items: list[dict[str, Any]],
    *,
    intervals: dict[str, float] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Validate and apply each item on its own, so one bad item never sinks the batch."""
    if len(items) > MAX_BATCH_ITEMS:
        raise ValueError(f"a batch holds at most {MAX_BATCH_ITEMS} items")
    now = now or utc_now()
    intervals = intervals or {}
    results = []
    for index, raw in enumerate(items):
        try:
            item = ITEM_ADAPTER.validate_python(raw)
            source_key = (
                item.record.source_key if isinstance(item, ObservationItem) else item.source_key
            )
            if source_key in NON_RETAIL_SOURCES:
                # Demo and practice rows live in their own data dirs, never a real catalogue.
                raise ValueError(f"{source_key} is not a retailer source")
            interval = effective_interval(source_key, intervals.get(source_key))
            if isinstance(item, ObservationItem):
                result = apply_observation(engine, item, interval_hours=interval, now=now)
            elif isinstance(item, NotModifiedItem):
                result = apply_not_modified(engine, item, interval_hours=interval, now=now)
            elif isinstance(item, FailureItem):
                result = apply_failure(engine, item, now=now)
            else:
                result = apply_missing(engine, item, now=now)
            results.append({"index": index, "source_key": source_key, **result})
        except (ValidationError, ValueError, TypeError) as exc:
            source_key = raw.get("source_key") if isinstance(raw, dict) else None
            if source_key is None and isinstance(raw, dict) and isinstance(raw.get("record"), dict):
                source_key = raw["record"].get("source_key")
            results.append(
                {
                    "index": index,
                    "source_key": source_key if source_key in SOURCES else None,
                    "status": "rejected",
                    "reason": _reason(exc),
                }
            )
    return results


def summarise(results: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, dict]:
    """Per-source counts of results and failure reasons for audit and cycle reports."""
    summary: dict[str, dict] = {}
    for result in results:
        key = result.get("source_key") or "unknown"
        entry = summary.setdefault(
            key, {"counts": Counter(), "rejection_reasons": Counter(), "failures": Counter()}
        )
        entry["counts"][result["status"]] += 1
        if result["status"] == "rejected":
            entry["rejection_reasons"][result["reason"]] += 1
        raw = items[result["index"]] if result["index"] < len(items) else {}
        if isinstance(raw, dict) and raw.get("type") in {"failure", "missing"}:
            reason = raw.get("reason") or MISSING_FROM_LISTING
            entry["failures"][str(reason)[:80]] += 1
    return summary


def cycle_status(report: dict[str, Any]) -> str:
    """success: everything due was checked cleanly; partial: work left or failures."""
    if report.get("stopped"):
        return "failed"
    if report.get("due_remaining") or report.get("failures_by_reason"):
        return "partial"
    return "success"


def save_run_row(
    engine: Database,
    *,
    source_key: str,
    kind: str,
    started_at: datetime,
    counts: Counter | dict,
    errors: dict[str, int],
    details: dict[str, Any],
    run_id: str | None = None,
    status: str | None = None,
    finished_at: datetime | None = None,
) -> str:
    """One CollectionRun row: the audit entry of an ingest batch or a refresh cycle."""
    _, _, Run = _models()
    counts = Counter(counts)
    total = sum(counts.values())
    valid = total - counts["rejected"]
    run_id = run_id or f"{kind}-{uuid4().hex[:16]}-{source_key}"
    Run.objects.using(engine.alias).create(
        run_id=run_id[:255],
        source_key=source_key,
        market_code="HU",
        started_at=started_at.astimezone(UTC).isoformat(),
        finished_at=(finished_at or utc_now()).astimezone(UTC).isoformat(),
        status=status
        or ("success" if valid and not counts["rejected"] else "partial" if valid else "failed"),
        records_discovered=total,
        records_parsed=total,
        records_seen=total,
        records_valid=valid,
        records_rejected=counts["rejected"],
        records_upserted=counts["created"] + counts["updated"],
        records_deduplicated=counts["unchanged"],
        error_summary=json.dumps(errors, ensure_ascii=False),
        completeness=json.dumps({"kind": kind, **details}, ensure_ascii=False, default=str),
        adapter_version=f"{kind}-v1",
    )
    return run_id


def save_cycle_rows(
    engine: Database, report: dict[str, Any], *, actor: str | None = None
) -> list[str]:
    """Store a runner's cycle report: one ``refresh-cycle`` row per retailer."""
    started = _parse_time(report.get("started_at")) or utc_now()
    finished = _parse_time(report.get("finished_at")) or utc_now()
    rows = []
    for source_key, entry in (report.get("sources") or {}).items():
        if source_key not in SOURCES or not isinstance(entry, dict):
            continue
        rows.append(
            save_run_row(
                engine,
                source_key=source_key,
                kind="refresh-cycle",
                started_at=started,
                finished_at=finished,
                counts=entry.get("results") or {},
                errors=entry.get("failures_by_reason") or {},
                details={"actor": actor, "client_run_id": report.get("run_id"), **entry},
                status=cycle_status(entry),
            )
        )
    return rows


def _parse_time(value: Any) -> datetime | None:
    try:
        return _aware(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


def recent_runs(engine: Database, *, source_key: str | None = None, limit: int = 20) -> list[dict]:
    _, _, Run = _models()
    query = Run.objects.using(engine.alias).filter(
        adapter_version__in=["refresh-cycle-v1", "api-ingest-v1", "refresh-v1"]
    )
    if source_key:
        query = query.filter(source_key=source_key)
    rows = []
    for row in query.order_by("-started_at").values()[:limit]:
        row["error_summary"] = json.loads(row["error_summary"] or "{}")
        row["completeness"] = json.loads(row["completeness"] or "{}")
        rows.append(row)
    return rows


def mark_observed(
    engine: Database, record: RawProductRecord, offer: RetailerOffer, *, now: datetime | None = None
) -> None:
    """Schedule the next refresh of an offer the discovery pipeline just stored.

    A first observation is staggered across the interval so a large discovery run
    does not make every offer due at the same moment.
    """
    _, Offer, _ = _models()
    stored = _find(Offer, engine, offer)
    if stored is None:
        return
    checked = min(_aware(record.fetched_at), now or utc_now())
    interval = configured_interval(offer.source_key)
    first = offer.canonical_url if stored.next_refresh_at is None else None
    fields = _success_fields(checked, interval, first_url=first)
    listing_url = _listing_refresh_url(record, offer)
    if listing_url:
        fields["refresh_url"] = listing_url
    Offer.objects.using(engine.alias).filter(pk=stored.pk).update(**fields)

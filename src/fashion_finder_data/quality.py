"""Read-only completeness and freshness checks for the local catalogue."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .collectors import SOURCES, allowed_url
from .schema import canonical_url, utc_now
from .storage import offers, retailers


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _metrics(rows: list[dict[str, Any]], now: datetime, cutoff: datetime) -> dict[str, Any]:
    counts = {
        "offers": len(rows),
        "stale": 0,
        "future_seen": 0,
        "missing_title": 0,
        "unmapped_category": 0,
        "missing_price": 0,
        "missing_image": 0,
        "unknown_availability": 0,
        "malformed_url": 0,
        "malformed_price": 0,
        "malformed_currency": 0,
        "malformed_timestamp": 0,
        "malformed_image_data": 0,
    }
    for row in rows:
        try:
            seen_at = _timestamp(row["last_seen_at"])
        except (TypeError, ValueError):
            counts["malformed_timestamp"] += 1
        else:
            counts["stale"] += int(seen_at < cutoff)
            counts["future_seen"] += int(seen_at > now)
        counts["missing_title"] += int(not (row["title"] or "").strip())
        counts["unmapped_category"] += int(bool(row["source_category"]) and row["category"] is None)
        counts["missing_price"] += int(row["price_amount"] is None or row["currency"] is None)
        counts["unknown_availability"] += int(row["availability_status"] == "unknown")
        source = SOURCES.get(row["source_key"])
        counts["malformed_url"] += int(
            source is None
            or not allowed_url(source, row["source_url"])
            or not allowed_url(source, row["canonical_url"])
        )
        counts["malformed_currency"] += int(
            row["currency"] is not None and re.fullmatch(r"[A-Z]{3}", row["currency"]) is None
        )
        if row["price_amount"] is not None:
            try:
                amount = Decimal(row["price_amount"])
                if not amount.is_finite() or amount < 0:
                    counts["malformed_price"] += 1
            except (InvalidOperation, TypeError):
                counts["malformed_price"] += 1
        try:
            images = json.loads(row["image_urls"])
            if not isinstance(images, list):
                raise TypeError("image data is not a list")
            for image in images:
                if not isinstance(image, str):
                    raise TypeError("image URL is not a string")
                canonical_url(image)
        except (TypeError, ValueError):
            counts["malformed_image_data"] += 1
            images = []
        counts["missing_image"] += int(not images)
    total = counts["offers"]
    counts["completeness"] = {
        "title": round((total - counts["missing_title"]) / total, 3) if total else 0.0,
        "price": round((total - counts["missing_price"]) / total, 3) if total else 0.0,
        "image": round((total - counts["missing_image"]) / total, 3) if total else 0.0,
    }
    return counts


def quality_report(
    engine: Engine, *, stale_after_days: int = 7, as_of: datetime | None = None
) -> dict[str, Any]:
    """Summarize stored offers; old observations never imply removal or stock state."""
    if not 1 <= stale_after_days <= 3650:
        raise ValueError("stale-after-days must be between 1 and 3650")
    now = as_of or utc_now()
    if now.tzinfo is None:
        raise ValueError("as-of timestamp must have a timezone")
    now = now.astimezone(UTC)
    cutoff = now - timedelta(days=stale_after_days)
    with engine.connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                select(offers, retailers.c.source_key, retailers.c.market_code).join(
                    retailers, offers.c.retailer_id == retailers.c.id
                )
            ).mappings()
        ]
    grouped: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["source_key"], row["category"]), []).append(row)
    return {
        "generated_at": now.isoformat(),
        "stale_after_days": stale_after_days,
        "totals": _metrics(rows, now, cutoff),
        "groups": [
            {
                "source_key": source,
                "category": category,
                **_metrics(group_rows, now, cutoff),
            }
            for (source, category), group_rows in sorted(
                grouped.items(), key=lambda item: (item[0][0], item[0][1] or "")
            )
        ],
    }

"""Query the local normalized catalogue with exact currency-aware prices."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .storage import offers, retailers


def search_catalogue(
    engine: Engine,
    *,
    query: str | None = None,
    source: str | None = None,
    category: str | None = None,
    currency: str = "HUF",
    max_price: Decimal | None = None,
    sort: str = "price-asc",
    limit: int = 20,
) -> list[dict]:
    """Return active offers; filter and sort money with Decimal, never float."""
    if max_price is not None and (not max_price.is_finite() or max_price < 0):
        raise ValueError("max price must be finite and nonnegative")
    currency = currency.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency must be a three-letter code")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if sort not in {"price-asc", "price-desc", "recent"}:
        raise ValueError("unsupported sort order")
    statement = (
        select(offers, retailers.c.source_key)
        .join(retailers, offers.c.retailer_id == retailers.c.id)
        .where(offers.c.status == "active", offers.c.currency == currency)
    )
    if source:
        statement = statement.where(retailers.c.source_key == source)
    if category:
        statement = statement.where(offers.c.category == category)
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(statement).mappings()]
    words = (query or "").casefold().split()
    results = []
    for row in rows:
        searchable = " ".join(
            str(row.get(key) or "") for key in ("title", "brand", "description")
        ).casefold()
        if any(word not in searchable for word in words):
            continue
        price = Decimal(row["price_amount"]) if row["price_amount"] is not None else None
        if max_price is not None and (price is None or price > max_price):
            continue
        results.append((row, price))
    if sort == "recent":

        def seen_at(row: dict) -> datetime:
            value = datetime.fromisoformat(row["last_seen_at"])
            return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)

        results.sort(key=lambda item: (seen_at(item[0]), item[0]["id"]), reverse=True)
    elif sort == "price-desc":
        results.sort(
            key=lambda item: (
                item[1] is None,
                -(item[1] or Decimal(0)),
                item[0]["id"],
            )
        )
    else:
        results.sort(
            key=lambda item: (
                item[1] is None,
                item[1] or Decimal(0),
                item[0]["id"],
            )
        )
    return [
        {
            "source_key": row["source_key"],
            "source_product_id": row["source_product_id"],
            "title": row["title"],
            "category": row["category"],
            "price_amount": str(price) if price is not None else None,
            "currency": row["currency"],
            "source_url": row["source_url"],
            "image_urls": json.loads(row["image_urls"]),
            "availability_status": row["availability_status"],
            "last_seen_at": row["last_seen_at"],
        }
        for row, price in results[:limit]
    ]

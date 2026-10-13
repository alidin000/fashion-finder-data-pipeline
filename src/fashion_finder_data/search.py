"""Query the local normalized catalogue with exact currency-aware prices."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal

from .storage import Database, offer_rows
from .text_match import matches_terms, query_terms, relevance, row_text


def search_catalogue(
    engine: Database,
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
    if sort not in {"price-asc", "price-desc", "recent", "relevance"}:
        raise ValueError("unsupported sort order")
    rows = offer_rows(engine, status="active", currency=currency)
    if source:
        rows = [row for row in rows if row["source_key"] == source]
    if category:
        rows = [row for row in rows if row["category"] == category]
    terms = query_terms(query)
    results = []
    for row in rows:
        if not matches_terms(terms, row_text(row)):
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
    elif sort == "relevance":
        results.sort(
            key=lambda item: (
                -relevance(terms, item[0]),
                item[1] is None,
                item[1] or Decimal(0),
                item[0]["id"],
            )
        )
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
            "source_category": row["source_category"],
            "price_amount": str(price) if price is not None else None,
            "currency": row["currency"],
            "source_url": row["source_url"],
            "image_urls": json.loads(row["image_urls"]),
            "availability_status": row["availability_status"],
            "last_seen_at": row["last_seen_at"],
        }
        for row, price in results[:limit]
    ]

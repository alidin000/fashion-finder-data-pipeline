"""Policy-aware read model for the shopper application."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db.models import Q

from .schema import is_hungarian_source_url
from .source_policy import SourcePolicy
from .storage import Database, offer_rows


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _visible(
    row: dict[str, Any], policies: dict[tuple[str, str], SourcePolicy], now: datetime
) -> SourcePolicy | None:
    policy = policies.get((row["source_key"], row["market_code"]))
    if policy is None or policy.approval_status != "approved" or not policy.allow_public_listing:
        return None
    if (
        row["status"] != "active"
        or row["availability_status"] == "out_of_stock"
        or row["currency"] != "HUF"
        or row["price_amount"] is None
        or not is_hungarian_source_url(row["source_key"], row["source_url"])
    ):
        return None
    try:
        price = Decimal(row["price_amount"])
        checked_at = _utc(row["source_fetched_at"])
        colours = json.loads(row["colour_names"])
        sizes = json.loads(row["size_options"])
        images = json.loads(row["image_urls"]) if policy.allow_image_display else []
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not price.is_finite() or price < 0:
        return None
    if any(
        not isinstance(values, list) or any(not isinstance(item, str) for item in values)
        for values in (colours, sizes, images)
    ):
        return None
    if checked_at > now or checked_at + timedelta(hours=policy.freshness_hours) < now:
        return None
    return policy


def _public_offer(row: dict[str, Any], policy: SourcePolicy) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source_key": row["source_key"],
        "source_product_id": row["source_product_id"],
        "title": row["title"],
        "category": row["category"],
        "price_amount": row["price_amount"],
        "currency": row["currency"],
        "source_url": row["source_url"],
        "image_urls": json.loads(row["image_urls"]) if policy.allow_image_display else [],
        "colour_names": json.loads(row["colour_names"]),
        "size_options": json.loads(row["size_options"]),
        "availability_status": row["availability_status"],
        "last_checked_at": row["source_fetched_at"],
        "match_type": "style_inspiration",
        "attribution": policy.attribution,
    }


def _approved_pairs(
    policies: dict[tuple[str, str], SourcePolicy], source: str | None = None
) -> list[tuple[str, str]]:
    return [
        (key, market)
        for (key, market), policy in policies.items()
        if policy.approval_status == "approved"
        and policy.allow_public_listing
        and (source is None or key == source)
    ]


def _rows(
    engine: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    *,
    source: str | None = None,
    category: str | None = None,
    offer_id: int | None = None,
) -> list[dict[str, Any]]:
    pairs = _approved_pairs(policies, source)
    if not pairs:
        return []
    approved = Q()
    for key, market in pairs:
        approved |= Q(retailer__source_key=key, retailer__market_code=market)
    filters: dict[str, Any] = {
        "status": "active",
        "currency": "HUF",
        "price_amount__isnull": False,
    }
    if category:
        filters["category"] = category
    if offer_id is not None:
        filters["id"] = offer_id
    return offer_rows(
        engine,
        predicate=approved & ~Q(availability_status="out_of_stock"),
        **filters,
    )


def public_offer(
    engine: Database,
    offer_id: int,
    policies: dict[tuple[str, str], SourcePolicy],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    current = now or datetime.now(UTC)
    for row in _rows(engine, policies, offer_id=offer_id):
        policy = _visible(row, policies, current)
        return _public_offer(row, policy) if policy else None
    return None


def search_public_catalogue(
    engine: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    *,
    query: str | None = None,
    category: str | None = None,
    colour: str | None = None,
    size: str | None = None,
    max_price: Decimal | None = None,
    source: str | None = None,
    sort: str = "price-asc",
    limit: int = 20,
    cursor: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if sort not in {"price-asc", "price-desc", "recent"}:
        raise ValueError("unsupported sort order")
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    if max_price is not None and (not max_price.is_finite() or max_price < 0):
        raise ValueError("max price must be finite and nonnegative")
    filters = [query, category, colour, size, str(max_price), source, sort]
    fingerprint = hashlib.sha256(json.dumps(filters).encode()).hexdigest()[:16]
    current = now or datetime.now(UTC)
    matches: list[tuple[dict[str, Any], SourcePolicy]] = []
    for row in _rows(engine, policies, source=source, category=category):
        policy = _visible(row, policies, current)
        if policy is None:
            continue
        if source and row["source_key"] != source:
            continue
        if category and row["category"] != category:
            continue
        price = Decimal(row["price_amount"])
        if max_price is not None and price > max_price:
            continue
        colours = json.loads(row["colour_names"])
        sizes = json.loads(row["size_options"])
        if colour and not any(colour.casefold() == item.casefold() for item in colours):
            continue
        if size and not any(size.casefold() == item.casefold() for item in sizes):
            continue
        searchable = " ".join(
            str(row.get(field) or "") for field in ("title", "brand", "description")
        ).casefold()
        if query and any(word not in searchable for word in query.casefold().split()):
            continue
        matches.append((row, policy))
    if sort == "recent":
        matches.sort(
            key=lambda item: (-_utc(item[0]["source_fetched_at"]).timestamp(), item[0]["id"])
        )
    elif sort == "price-desc":
        matches.sort(key=lambda item: (-Decimal(item[0]["price_amount"]), item[0]["id"]))
    else:
        matches.sort(key=lambda item: (Decimal(item[0]["price_amount"]), item[0]["id"]))
    start = 0
    if cursor:
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if payload["f"] != fingerprint:
                raise ValueError("cursor does not match filters")
            start = next(i + 1 for i, item in enumerate(matches) if item[0]["id"] == payload["id"])
        except (ValueError, KeyError, TypeError, StopIteration, binascii.Error) as exc:
            raise ValueError("invalid or expired cursor") from exc
    page = matches[start : start + limit]
    next_cursor = None
    if start + limit < len(matches):
        payload = {"f": fingerprint, "id": page[-1][0]["id"]}
        next_cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return {
        "items": [_public_offer(row, policy) for row, policy in page],
        "next_cursor": next_cursor,
    }

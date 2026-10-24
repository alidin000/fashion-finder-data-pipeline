"""Import a retailer-provided Hungarian product CSV into the shared pipeline."""

from __future__ import annotations

import csv
import re
from datetime import UTC, datetime
from pathlib import Path

from .pipeline import ingest_records
from .schema import RawProductRecord, normalize, utc_now
from .sources import SOURCES, allowed_url
from .storage import Database

REQUIRED_COLUMNS = {"product_url", "name", "price", "currency"}
MAX_FEED_BYTES = 20 * 1024 * 1024


def _read_feed(
    path: Path,
    source_key: str,
    *,
    require_observed_at: bool,
) -> tuple[list[tuple[int, RawProductRecord]], list[str], int]:
    """Parse one canonical CSV without changing persistent state."""
    source = SOURCES[source_key]
    if path.stat().st_size > MAX_FEED_BYTES:
        raise ValueError("feed exceeds the 20 MB limit")
    records: list[tuple[int, RawProductRecord]] = []
    errors: list[str] = []
    discovered = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if require_observed_at and "observed_at" not in (reader.fieldnames or []):
            missing.add("observed_at")
        if missing:
            raise ValueError(f"feed is missing columns: {', '.join(sorted(missing))}")
        for number, row in enumerate(reader, 2):
            discovered += 1
            if None in row:
                errors.append(f"feed row {number}: extra columns")
                continue
            url = (row["product_url"] or "").strip()
            name = (row["name"] or "").strip()
            price = (row["price"] or "").strip()
            currency = (row["currency"] or "").strip().upper()
            if not allowed_url(source, url):
                errors.append(f"feed row {number}: URL is outside the Hungarian storefront")
                continue
            if not name or not price or currency != "HUF":
                errors.append(f"feed row {number}: name, price and HUF currency are required")
                continue
            observed = row.get("observed_at")
            if "observed_at" in (reader.fieldnames or []):
                try:
                    fetched_at = datetime.fromisoformat((observed or "").strip())
                    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
                        raise ValueError("timezone required")
                    fetched_at = fetched_at.astimezone(UTC)
                    if fetched_at > utc_now():
                        raise ValueError("future observation")
                except ValueError:
                    errors.append(f"feed row {number}: invalid observed_at timestamp")
                    continue
            else:
                fetched_at = utc_now()
            product = {
                "url": url,
                "sku": row.get("sku"),
                "name": name,
                "price": price,
                "currency": currency,
                "brand": row.get("brand"),
                "category": row.get("category") or None,
                "description": row.get("description"),
                "original_price": row.get("original_price"),
                "images": ([row["image_url"].strip()] if row.get("image_url") else [])
                + (row.get("additional_image_urls") or "").split(),
                "colours": row.get("colour"),
                "sizes": [size for size in (row.get("size") or "").split("|") if size.strip()],
                "availability": row.get("availability"),
                "item_group_id": row.get("item_group_id") or None,
            }
            records.append(
                (
                    number,
                    RawProductRecord(
                        source_key=source_key,
                        market_code="HU",
                        source_url=url,
                        fetched_at=fetched_at,
                        adapter_version=(
                            "feed-csv-v2"
                            if "observed_at" in (reader.fieldnames or [])
                            else "feed-csv-v1"
                        ),
                        product=product,
                    ),
                )
            )
    return records, errors, discovered


def row_problems(path: Path, source_key: str) -> set[int]:
    """CSV line numbers that a timestamped feed import would reject."""
    records, errors, _ = _read_feed(path, source_key, require_observed_at=True)
    bad = {int(match[1]) for error in errors if (match := re.match(r"feed row (\d+):", error))}
    for number, record in records:
        try:
            offer = normalize(record)
            if offer.price_amount is None or offer.currency != "HUF":
                raise ValueError("HUF price is required")
        except (TypeError, ValueError):
            bad.add(number)
    return bad


def validate_feed(path: Path, source_key: str) -> dict:
    """Check a timestamped canonical feed without importing or approving it."""
    records, errors, discovered = _read_feed(path, source_key, require_observed_at=True)
    valid = 0
    for number, record in records:
        try:
            offer = normalize(record)
            if offer.price_amount is None or offer.currency != "HUF":
                raise ValueError("HUF price is required")
        except (TypeError, ValueError) as exc:
            errors.append(f"feed row {number}: {exc}")
        else:
            valid += 1
    return {
        "source_key": source_key,
        "status": "valid" if valid > 0 and not errors else "invalid",
        "records_discovered": discovered,
        "records_valid": valid,
        "records_rejected": discovered - valid,
        "errors": errors[:20],
    }


def import_feed(
    engine: Database,
    root: Path,
    path: Path,
    source_key: str,
    *,
    require_observed_at: bool = False,
) -> dict:
    """Import a local, authorized CSV; reject bad rows without losing good ones."""
    from .scheduler import mark_observed

    numbered, errors, discovered = _read_feed(
        path, source_key, require_observed_at=require_observed_at
    )
    return ingest_records(
        engine,
        root,
        source_key,
        [record for _, record in numbered],
        errors,
        discovered=discovered,
        preparse_rejected=len(errors),
        # A feed row is a check of that offer: schedule its next web refresh, if any.
        on_offer=lambda record, offer, outcome: mark_observed(engine, record, offer),
    )

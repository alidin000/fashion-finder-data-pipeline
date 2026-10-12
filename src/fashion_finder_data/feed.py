"""Import a retailer-provided Hungarian product CSV into the shared pipeline."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from .collectors import SOURCES, allowed_url
from .pipeline import ingest_records
from .schema import RawProductRecord, utc_now
from .storage import Database

REQUIRED_COLUMNS = {"product_url", "name", "price", "currency"}


def import_feed(
    engine: Database,
    root: Path,
    path: Path,
    source_key: str,
    *,
    require_observed_at: bool = False,
) -> dict:
    """Import a local, authorized CSV; reject bad rows without losing good ones."""
    source = SOURCES[source_key]
    records: list[RawProductRecord] = []
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
                "images": [row["image_url"].strip()] if row.get("image_url") else [],
                "colours": row.get("colour"),
                "sizes": row.get("size"),
                "availability": row.get("availability"),
            }
            records.append(
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
                )
            )
    return ingest_records(
        engine,
        root,
        source_key,
        records,
        errors,
        discovered=discovered,
        preparse_rejected=len(errors),
    )

"""Quality reports must expose missing and stale data without changing offers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from fashion_finder_data.pipeline import ingest_records
from fashion_finder_data.quality import quality_report
from fashion_finder_data.schema import RawProductRecord
from fashion_finder_data.storage import offers, open_database


def record(sku: str, fetched_at: datetime, *, price: str | None, images: list[str]):
    url = f"https://www.reserved.com/hu/hu/example-coat-{sku.lower()}-45c"
    return RawProductRecord(
        source_key="reserved",
        source_url=url,
        fetched_at=fetched_at,
        product={
            "url": url,
            "sku": sku,
            "name": "Example coat",
            "category": "coats",
            "price": price,
            "currency": "HUF" if price is not None else None,
            "images": images,
        },
    )


def test_quality_groups_and_flags_old_missing_offers_without_marking_removed(
    tmp_path: Path,
) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    now = datetime(2026, 10, 12, tzinfo=UTC)
    ingest_records(
        engine,
        tmp_path,
        "reserved",
        [
            record("FRESH", now, price="25995", images=["https://static.reserved.com/a.jpg"]),
            record("OLD", now - timedelta(days=10), price=None, images=[]),
        ],
    )
    result = quality_report(engine, as_of=now, stale_after_days=7)
    assert result["totals"]["offers"] == 2
    assert result["totals"]["stale"] == 1
    assert result["totals"]["missing_price"] == 1
    assert result["totals"]["missing_image"] == 1
    assert result["groups"][0]["source_key"] == "reserved"
    assert result["groups"][0]["category"] == "coats"
    with engine.connect() as connection:
        statuses = connection.execute(select(offers.c.status)).scalars().all()
    assert statuses == ["active", "active"]


def test_quality_detects_malformed_legacy_values(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    now = datetime(2026, 10, 12, tzinfo=UTC)
    ingest_records(engine, tmp_path, "reserved", [record("BAD", now, price="25995", images=[])])
    with engine.begin() as connection:
        connection.execute(
            offers.update().values(
                source_url="http://example.com/wrong-market",
                price_amount="NaN",
                currency="huf!",
                last_seen_at="not-a-date",
                image_urls="{",
            )
        )
    counts = quality_report(engine, as_of=now)["totals"]
    assert counts["malformed_url"] == 1
    assert counts["malformed_price"] == 1
    assert counts["malformed_currency"] == 1
    assert counts["malformed_timestamp"] == 1
    assert counts["malformed_image_data"] == 1
    with engine.begin() as connection:
        connection.execute(offers.update().values(image_urls=json.dumps(["javascript:bad"])))
    assert quality_report(engine, as_of=now)["totals"]["malformed_image_data"] == 1

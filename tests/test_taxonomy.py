"""Canonical categories preserve source labels and migrate existing SQLite data."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from django.db import connections

from fashion_finder_data.catalogue.models import RetailerOffer
from fashion_finder_data.pipeline import ingest_records
from fashion_finder_data.quality import quality_report
from fashion_finder_data.schema import RawProductRecord, normalize
from fashion_finder_data.storage import open_database


def raw(category: str) -> RawProductRecord:
    url = "https://www.reserved.com/hu/hu/example-coat-123ab-45c"
    return RawProductRecord(
        source_key="reserved",
        source_url=url,
        fetched_at=datetime(2026, 10, 12, tzinfo=UTC),
        product={
            "url": url,
            "sku": "123AB-45C",
            "name": "Example coat",
            "price": "25995",
            "currency": "HUF",
            "category": category,
        },
    )


def test_known_hungarian_category_maps_and_unknown_is_preserved(tmp_path: Path) -> None:
    mapped = normalize(raw("Kabátok"))
    assert mapped.category == "coats"
    assert mapped.source_category == "Kabátok"
    unknown = normalize(raw("Ceremonial capes"))
    assert unknown.category is None
    assert unknown.source_category == "Ceremonial capes"
    engine = open_database(tmp_path / "catalog.sqlite")
    ingest_records(engine, tmp_path, "reserved", [raw("Ceremonial capes")])
    report = quality_report(engine, as_of=datetime(2026, 10, 12, tzinfo=UTC))
    assert report["totals"]["unmapped_category"] == 1


def test_existing_sqlite_categories_migrate_without_losing_original(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    engine = open_database(path)
    ingest_records(engine, tmp_path, "reserved", [raw("Kabátok")])
    connections[engine.alias].close()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE retailer_offer DROP COLUMN source_category")
        connection.execute("UPDATE retailer_offer SET category = ?", ("Kabátok",))
    migrated = open_database(path)
    row = (
        RetailerOffer.objects.using(migrated.alias).values_list("category", "source_category").get()
    )
    assert row == ("coats", "Kabátok")


def test_sparse_refresh_keeps_last_known_category(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    first = raw("Kabátok")
    ingest_records(engine, tmp_path, "reserved", [first])
    later = raw("Kabátok")
    later.product.pop("category")
    ingest_records(engine, tmp_path, "reserved", [later])
    row = RetailerOffer.objects.using(engine.alias).values_list("category", "source_category").get()
    assert row == ("coats", "Kabátok")

"""Search contracts for local offers and exact currency-aware budgets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from fashion_finder_data.pipeline import ingest_records
from fashion_finder_data.schema import RawProductRecord
from fashion_finder_data.search import search_catalogue
from fashion_finder_data.storage import open_database


def record(source: str, url: str, sku: str, title: str, price: str, currency: str, category: str):
    return RawProductRecord(
        source_key=source,
        source_url=url,
        fetched_at=datetime(2026, 10, 12, tzinfo=UTC),
        product={
            "url": url,
            "sku": sku,
            "name": title,
            "price": price,
            "currency": currency,
            "category": category,
        },
    )


def test_search_filters_text_category_budget_and_currency(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    ingest_records(
        engine,
        tmp_path,
        "reserved",
        [
            record(
                "reserved",
                "https://www.reserved.com/hu/hu/wool-coat-123ab-45c",
                "C1",
                "Gyapjúkabát",
                "25995",
                "HUF",
                "coats",
            ),
            record(
                "reserved",
                "https://www.reserved.com/hu/hu/blue-dress-234ab-45c",
                "D1",
                "Kék ruha",
                "39995",
                "HUF",
                "dresses",
            ),
            record(
                "reserved",
                "https://www.reserved.com/hu/hu/eur-dress-345ab-45c",
                "D2",
                "Másik ruha",
                "19.99",
                "EUR",
                "dresses",
            ),
        ],
    )
    ingest_records(
        engine,
        tmp_path,
        "hm",
        [
            record(
                "hm",
                "https://www2.hm.com/hu_hu/productpage.1234567890.html",
                "J1",
                "Olcsó dzseki",
                "9995",
                "HUF",
                "jackets",
            )
        ],
    )
    assert [
        item["source_product_id"] for item in search_catalogue(engine, max_price=Decimal(30000))
    ] == [
        "J1",
        "C1",
    ]
    assert [item["source_product_id"] for item in search_catalogue(engine, sort="price-desc")] == [
        "D1",
        "C1",
        "J1",
    ]
    assert [
        item["source_product_id"]
        for item in search_catalogue(engine, query="GYAPJÚ", category="coats", source="reserved")
    ] == ["C1"]
    assert [item["source_product_id"] for item in search_catalogue(engine, currency="EUR")] == [
        "D2"
    ]


def test_search_rejects_invalid_budget(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    with pytest.raises(ValueError, match="nonnegative"):
        search_catalogue(engine, max_price=Decimal("NaN"))
    with pytest.raises(ValueError, match="currency"):
        search_catalogue(engine, currency="HUF OR 1=1")


def test_recent_sort_uses_instant_across_timezones(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    older = record(
        "reserved",
        "https://www.reserved.com/hu/hu/old-coat-123ab-45c",
        "OLD",
        "Older coat",
        "10000",
        "HUF",
        "coats",
    )
    older.fetched_at = datetime(2026, 10, 12, 9, tzinfo=timezone(timedelta(hours=2)))
    newer = record(
        "reserved",
        "https://www.reserved.com/hu/hu/new-coat-234ab-45c",
        "NEW",
        "Newer coat",
        "10000",
        "HUF",
        "coats",
    )
    newer.fetched_at = datetime(2026, 10, 12, 8, tzinfo=UTC)
    ingest_records(engine, tmp_path, "reserved", [older, newer])
    assert [item["source_product_id"] for item in search_catalogue(engine, sort="recent")] == [
        "NEW",
        "OLD",
    ]

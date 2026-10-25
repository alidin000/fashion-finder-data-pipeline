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


def test_english_and_unaccented_queries_find_hungarian_listings(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    coat = record(
        "house",
        "https://www.housebrand.com/hu/hu/steppelt-kabat-774kf-mlc",
        "COAT",
        "Steppelt kapucnis kabát",
        "24995",
        "HUF",
        None,
    )
    coat.product["colours"] = ["fekete"]
    dress = record(
        "house",
        "https://www.housebrand.com/hu/hu/kek-ruha-511kf-55x",
        "DRESS",
        "Kék ruha",
        "14995",
        "HUF",
        "dresses",
    )
    denim = record(
        "house",
        "https://www.housebrand.com/hu/hu/farmerdzseki-622kf-55j",
        "DENIM",
        "Farmerdzseki",
        "19995",
        "HUF",
        None,
    )
    ingest_records(engine, tmp_path, "house", [coat, dress, denim])

    def found(query: str) -> list[str]:
        return [item["source_product_id"] for item in search_catalogue(engine, query=query)]

    assert found("black coat") == ["COAT"]  # colour and title are Hungarian
    assert found("kabat") == ["COAT"]  # accents are optional
    assert found("dresses") == ["DRESS"]  # English category key
    assert found("blue") == ["DRESS"]
    assert found("red coat") == []
    assert found("denim jacket") == ["DENIM"]
    assert found("jeans") == []  # "farmer" alone also names denim jackets
    # Who it is for is a hint, not a filter: shop titles rarely say "női" and offers have no
    # gender field, so these used to return nothing.
    assert found("női kabát") == ["COAT"]
    assert found("fekete női kabát") == ["COAT"]
    assert found("women's coat") == ["COAT"]
    assert found("női ruha") == ["DRESS"]


def test_whole_words_avoid_false_hits_and_hungarian_finds_english(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    reserved = [
        ("redozott-szoknya-111aa-45c", "PLEAT", "Redőzött szoknya", "skirts"),
        ("bagolymintas-polo-222bb-45c", "OWL", "Bagolymintás póló", None),
        ("gyapjukabat-333cc-45c", "WOOL", "Gyapjúkabát", None),
        ("kabat-444dd-45c", "PLAIN", "Kabát", None),
    ]
    ingest_records(
        engine,
        tmp_path,
        "reserved",
        [
            record(
                "reserved", f"https://www.reserved.com/hu/hu/{slug}", sku, title, "9995", "HUF", cat
            )
            for slug, sku, title, cat in reserved
        ],
    )
    ingest_records(
        engine,
        tmp_path,
        "zara",
        [
            record(
                "zara",
                "https://www.zara.com/hu/en/black-coat-p123.html",
                "ZARA",
                "Black coat",
                "29995",
                "HUF",
                None,
            )
        ],
    )

    def found(query: str, sort: str = "price-asc") -> list[str]:
        return [
            item["source_product_id"] for item in search_catalogue(engine, query=query, sort=sort)
        ]

    assert found("red") == []  # not "Redőzött" (pleated)
    assert found("bag") == []  # not "Bagolymintás" (owl print)
    assert "ZARA" in found("fekete kabát")  # Hungarian query, English title
    # Exact title words (Hungarian "Kabát", translated "coat") rank above a compound
    # that only ends in the word; equal scores fall back to price.
    assert found("kabát", sort="relevance") == ["PLAIN", "ZARA", "WOOL"]


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

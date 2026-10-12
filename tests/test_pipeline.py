"""Behaviour-focused checks for extraction, normalization and repeat runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from scrapy.http import HtmlResponse, Request
from sqlalchemy import func, select

from fashion_finder_data.collectors import (
    SOURCES,
    CrawlState,
    ProductSpider,
    extract_hm_cards,
    extract_product,
    product_links,
    source_for_page,
)
from fashion_finder_data.feed import import_feed
from fashion_finder_data.pipeline import ingest_jsonl, ingest_records
from fashion_finder_data.schema import RawProductRecord, normalize, parse_money
from fashion_finder_data.storage import export_catalogue, observations, offers, open_database

URL = "https://www.reserved.com/hu/hu/test-coat-123ab-45c"


def response(url: str, body: str) -> HtmlResponse:
    return HtmlResponse(url=url, body=body.encode(), encoding="utf-8", request=Request(url))


def raw(
    price: str | None = "42995.00", sku: str | None = "123AB-45C", url: str = URL
) -> RawProductRecord:
    return RawProductRecord(
        source_key="reserved",
        source_url=url,
        fetched_at=datetime(2026, 10, 12, tzinfo=UTC),
        product={
            "url": url,
            "sku": sku,
            "name": "Wool coat",
            "price": price,
            "currency": "HUF" if price is not None else None,
            "images": ["https://static.reserved.com/coat.jpg"],
        },
    )


def test_reserved_category_and_product_extract() -> None:
    category = response(
        "https://www.reserved.com/hu/hu/noi/ruhazat/kabatok",
        '<article class="es-product" data-sku="123AB-45C">'
        f'<a class="es-product-photo" href="{URL}">Coat</a></article>'
        '<a href="https://www.reserved.com/pl/pl/other-p123.html">Other market</a>',
    )
    assert product_links(category, SOURCES["reserved"], 1) == [URL]
    data = {
        "@type": "Product",
        "sku": "123AB-45C",
        "name": "Wool coat",
        "brand": {"name": "Reserved"},
        "image": "https://static.reserved.com/coat.jpg",
        "offers": {"url": URL, "price": "42 995", "priceCurrency": "HUF"},
    }
    product = response(URL, f'<script type="application/ld+json">{json.dumps(data)}</script>')
    offer = normalize(extract_product(product, SOURCES["reserved"]))
    assert offer.source_product_id == "123AB-45C"
    assert offer.price_amount == Decimal(42995)
    assert offer.currency == "HUF"
    assert offer.availability_status == "unknown"


def test_numeric_jsonld_price_remains_exact_and_can_be_archived(tmp_path: Path) -> None:
    data = {
        "@type": "Product",
        "sku": "123AB-45C",
        "name": "Wool coat",
        "offers": {"price": 129.99, "priceCurrency": "HUF"},
    }
    product = response(URL, f'<script type="application/ld+json">{json.dumps(data)}</script>')
    record = extract_product(product, SOURCES["reserved"])
    assert normalize(record).price_amount == Decimal("129.99")
    engine = open_database(tmp_path / "catalog.sqlite")
    report = ingest_records(engine, tmp_path, "reserved", [record])
    assert report["records_valid"] == 1
    assert (tmp_path / "raw" / "reserved" / f"{report['run_id']}.jsonl").exists()


def test_hm_category_card_extracts_offer_without_product_page() -> None:
    category = response(
        SOURCES["hm"].category_url,
        '<article data-articlecode="1369582001">'
        '<a href="/hu_hu/productpage.1369582001.html" title="Filcdzseki">'
        '<img data-src="https://image.hm.com/jacket.jpg"></a>'
        '<span translate="no">21\u00a0995 Ft</span></article>',
    )
    records = extract_hm_cards(category, SOURCES["hm"], 1)
    assert len(records) == 1
    offer = normalize(records[0])
    assert offer.source_product_id == "1369582001"
    assert offer.price_amount == Decimal(21995)
    assert offer.currency == "HUF"
    assert offer.source_url == "https://www2.hm.com/hu_hu/productpage.1369582001.html"
    state = CrawlState()
    spider = ProductSpider([SOURCES["hm"]], state, 1)
    assert list(spider.parse_page(category, SOURCES["hm"])) == []
    assert len(state.records["hm"]) == 1


def test_limit_is_shared_across_starting_categories_and_skips_duplicates() -> None:
    source = SOURCES["reserved"]
    first_url = "https://www.reserved.com/hu/hu/noi/kabatok"
    second_url = "https://www.reserved.com/hu/hu/noi/ruhak"
    links = [f"https://www.reserved.com/hu/hu/example-item-{number}ab-45c" for number in range(4)]

    def category(url: str, products: list[str]) -> HtmlResponse:
        body = "".join(
            f'<article class="es-product"><a class="es-product-photo" href="{product}"></a></article>'
            for product in products
        )
        return response(url, body)

    state = CrawlState()
    spider = ProductSpider([source], state, 3, overrides={"reserved": [first_url, second_url]})
    assert len(list(spider.start_requests())) == 2
    first = list(spider.parse_page(category(first_url, links[:2]), source))
    second = list(spider.parse_page(category(second_url, links[1:]), source))
    assert [request.url for request in first + second] == links[:3]
    assert state.discovered["reserved"] == 3
    assert list(spider.parse_page(category(second_url, links[1:]), source)) == []


def test_limit_also_bounds_direct_product_start_urls() -> None:
    source = SOURCES["reserved"]
    urls = [f"https://www.reserved.com/hu/hu/example-item-{number}ab-45c" for number in range(3)]
    state = CrawlState()
    spider = ProductSpider([source], state, 2, overrides={"reserved": urls})
    assert [request.url for request in spider.start_requests()] == urls[:2]
    assert state.discovered["reserved"] == 2


def test_product_extraction_error_is_preserved() -> None:
    source = SOURCES["reserved"]
    state = CrawlState()
    spider = ProductSpider([source], state, 1)
    assert list(spider.parse_page(response(URL, "<html></html>"), source)) == []
    assert state.errors["reserved"] == [
        "product extraction failed: no Product structured data or product price metadata"
    ]


def test_reserved_category_override_uses_matching_taxonomy() -> None:
    assert (
        source_for_page(SOURCES["reserved"], "https://www.reserved.com/hu/hu/noi/ruhak").category
        == "dresses"
    )
    assert source_for_page(SOURCES["reserved"], URL).category is None


def test_price_and_product_url_validation() -> None:
    assert parse_money("62,995 Ft", "HUF") == Decimal(62995)
    assert parse_money("19\u00a0995", "HUF") == Decimal(19995)
    assert parse_money("29,99", "EUR") == Decimal("29.99")
    with pytest.raises(ValueError, match="ambiguous"):
        parse_money("1,234", "EUR")
    with pytest.raises(ValueError, match="URL"):
        normalize(raw(url="javascript:alert(1)"))


def test_tracking_query_is_preserved_but_deduplicated(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    tracked = raw(sku=None, url=URL + "?utm_source=sample")
    offer = normalize(tracked)
    assert offer.source_url.endswith("?utm_source=sample")
    assert offer.canonical_url == URL
    ingest_records(engine, tmp_path, "reserved", [tracked])
    report = ingest_records(engine, tmp_path, "reserved", [raw(sku=None)])
    with engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(offers))
    assert report["records_valid"] == 1
    assert count == 1


def test_repeat_run_updates_offer_and_only_records_price_changes(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    first = ingest_records(engine, tmp_path, "reserved", [raw()])
    second = ingest_records(engine, tmp_path, "reserved", [raw("42995")])
    third = ingest_records(engine, tmp_path, "reserved", [raw("39995.00")])
    with engine.connect() as connection:
        offer_count = connection.scalar(select(func.count()).select_from(offers))
        prices = (
            connection.execute(select(observations.c.price_amount).order_by(observations.c.id))
            .scalars()
            .all()
        )
    assert first["records_upserted"] == 1
    assert second["records_deduplicated"] == 1
    assert third["records_upserted"] == 1
    assert offer_count == 1
    assert prices == ["42995", "39995"]
    assert (tmp_path / "raw" / "reserved" / f"{first['run_id']}.jsonl").exists()


def test_missing_price_does_not_erase_last_known_price(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    ingest_records(engine, tmp_path, "reserved", [raw()])
    ingest_records(engine, tmp_path, "reserved", [raw(None)])
    with engine.connect() as connection:
        price = connection.scalar(select(offers.c.price_amount))
        count = connection.scalar(select(func.count()).select_from(observations))
    assert price == "42995"
    assert count == 1


def test_equivalent_legacy_price_format_does_not_create_observation(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    ingest_records(engine, tmp_path, "reserved", [raw()])
    with engine.begin() as connection:
        connection.execute(offers.update().values(price_amount="42995.00"))
        connection.execute(observations.update().values(price_amount="42995.00"))
    report = ingest_records(engine, tmp_path, "reserved", [raw("42995")])
    with engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(observations))
    assert report["records_deduplicated"] == 1
    assert count == 1


def test_failed_run_is_reported_and_other_source_can_succeed(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    failed = ingest_records(engine, tmp_path, "zara", [], ["request failed: HTTP 403"])
    succeeded = ingest_records(engine, tmp_path, "reserved", [raw()])
    assert failed["status"] == "failed"
    assert succeeded["status"] == "success"
    assert succeeded["records_valid"] == 1


def test_conflicting_source_identity_is_rejected(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    ingest_records(engine, tmp_path, "reserved", [raw()])
    report = ingest_records(engine, tmp_path, "reserved", [raw(sku="different-sku")])
    with engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(offers))
    assert report["records_rejected"] == 1
    assert report["status"] == "failed"
    assert count == 1


def test_csv_export_escapes_spreadsheet_formulas(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    record = raw()
    record.product["name"] = "=1+1"
    ingest_records(engine, tmp_path, "reserved", [record])
    output = tmp_path / "catalogue.csv"
    assert export_catalogue(engine, output) == 1
    assert "'=1+1" in output.read_text(encoding="utf-8")


def test_authorized_feed_import_rejects_bad_market_and_preserves_good_rows(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    feed = tmp_path / "partner.csv"
    feed.write_text(
        "product_url,name,price,currency,sku,image_url\n"
        f"{URL},Wool coat,42 995,HUF,123AB-45C,https://static.reserved.com/coat.jpg\n"
        "https://www.reserved.com/pl/pl/coat,Wrong market,100,HUF,BAD,\n"
        f"{URL},Bad price,not-a-price,HUF,BAD2,\n",
        encoding="utf-8",
    )
    report = import_feed(engine, tmp_path, feed, "reserved")
    with engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(offers))
    assert report["status"] == "partial"
    assert report["records_discovered"] == 3
    assert report["records_valid"] == 1
    assert report["records_rejected"] == 2
    assert report["adapter_version"] == "feed-csv-v1"
    assert count == 1


def test_feed_requires_contract_columns(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    feed = tmp_path / "partner.csv"
    feed.write_text("product_url,name\n", encoding="utf-8")
    with pytest.raises(ValueError, match="currency, price"):
        import_feed(engine, tmp_path, feed, "zara")


def test_raw_replay_counts_malformed_lines(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "catalog.sqlite")
    snapshot = tmp_path / "raw.jsonl"
    snapshot.write_text(raw().model_dump_json() + "\nnot json\n", encoding="utf-8")
    report = ingest_jsonl(engine, tmp_path, snapshot, "reserved")
    assert report["status"] == "partial"
    assert report["records_discovered"] == 2
    assert report["records_seen"] == 2
    assert report["records_rejected"] == 1

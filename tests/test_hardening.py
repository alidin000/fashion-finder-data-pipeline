"""Regression checks for failures found in the October 2026 code review."""

from __future__ import annotations

import random
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, connections
from django.test import Client
from PIL import Image

from fashion_finder_data.analytics import metric_report, record_metric
from fashion_finder_data.api import views
from fashion_finder_data.api.views import api_context
from fashion_finder_data.catalogue.models import PriceObservation
from fashion_finder_data.catalogue.models import RetailerOffer as StoredOffer
from fashion_finder_data.collectors import SOURCES, lpp_record
from fashion_finder_data.demo import seed_demo
from fashion_finder_data.public_catalogue import search_public_catalogue
from fashion_finder_data.schema import RawProductRecord, RetailerOffer, normalize
from fashion_finder_data.source_policy import SourcePolicy
from fashion_finder_data.storage import offer_rows, open_database, upsert_offer

URL = "https://www.reserved.com/hu/hu/wool-coat-123ab-45c"
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
POLICIES = {
    ("reserved", "HU"): SourcePolicy(
        source_key="reserved",
        approval_status="approved",
        evidence_ref="local test fixture only",
        allow_public_listing=True,
    )
}


def offer(price: str, fetched_at: datetime, url: str = URL, **fields) -> RetailerOffer:
    return RetailerOffer(
        source_key="reserved",
        source_url=url,
        canonical_url=url,
        source_product_id=url.rsplit("-", 2)[-2],
        title="Gyapjúkabát",
        price_amount=Decimal(price),
        currency="HUF",
        source_fetched_at=fetched_at,
        **fields,
    )


def stored(database) -> StoredOffer:
    return StoredOffer.objects.using(database.alias).get()


def test_concurrent_writers_wait_instead_of_failing(tmp_path) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    errors: list[Exception] = []

    def write_events() -> None:
        try:
            for _ in range(20):
                event = {"event_type": "search", "search_type": "text", "result_count": 3}
                record_metric(database, event, {})
        except Exception as exc:  # noqa: BLE001 - the test collects any failure
            errors.append(exc)
        finally:
            connections[database.alias].close()

    threads = [threading.Thread(target=write_events) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    report = metric_report(database)
    assert sum(row["total"] for row in report["counters"]) == 160


def test_an_older_observation_arriving_late_only_fills_in_history(tmp_path) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    upsert_offer(database, offer("9995", NOW))
    assert upsert_offer(database, offer("12995", NOW - timedelta(days=2))) is False
    current = stored(database)
    assert current.price_amount == "9995"
    assert current.last_seen_at == NOW.isoformat()
    history = sorted(
        PriceObservation.objects.using(database.alias).values_list("observed_at", "price_amount")
    )
    assert [price for _, price in history] == ["12995", "9995"]


def test_missing_photos_and_unknown_stock_keep_what_is_stored(tmp_path) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    photos = ["https://static.reserved.com/coat.jpg"]
    upsert_offer(
        database,
        offer(
            "9995",
            NOW,
            image_urls=photos,
            colour_names=["fekete"],
            availability_status="out_of_stock",
        ),
    )
    upsert_offer(database, offer("9995", NOW + timedelta(hours=1)))  # a feed row without them
    current = stored(database)
    assert current.image_urls == '["https://static.reserved.com/coat.jpg"]'
    assert current.colour_names == '["fekete"]'
    assert current.availability_status == "out_of_stock"


def test_paging_continues_after_an_item_on_the_last_page_sells_out(tmp_path) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    now = datetime.now(UTC)
    for code, price in (("111aa", "1000"), ("222bb", "2000"), ("333cc", "3000")):
        upsert_offer(
            database, offer(price, now, url=f"https://www.reserved.com/hu/hu/coat-{code}-45c")
        )
    first = search_public_catalogue(database, POLICIES, limit=1, now=now)
    assert [item["price_amount"] for item in first["items"]] == ["1000"]
    StoredOffer.objects.using(database.alias).filter(price_amount="1000").update(
        availability_status="out_of_stock"
    )
    second = search_public_catalogue(
        database, POLICIES, limit=1, cursor=first["next_cursor"], now=now
    )
    assert [item["price_amount"] for item in second["items"]] == ["2000"]


def test_unusual_photo_values_do_not_reject_the_offer() -> None:
    record = RawProductRecord(
        source_key="reserved",
        source_url=URL,
        product={
            "url": URL,
            "name": "Gyapjúkabát",
            "price": "9995",
            "currency": "HUF",
            "images": [
                {"@type": "ImageObject", "url": "https://static.reserved.com/a.jpg"},
                "/media/b.jpg",
                "javascript:alert(1)",
                "https://static.reserved.com/a.jpg",
            ],
        },
    )
    assert normalize(record).image_urls == [
        "https://static.reserved.com/a.jpg",
        "https://www.reserved.com/media/b.jpg",
    ]


def test_lpp_null_price_falls_back_and_integer_stock_is_not_in_stock() -> None:
    item = {
        "sku": "123AB-45C",
        "name": "Kabát",
        "url": URL,
        "currency": "HUF",
        "minQtyFinalPrice": None,
        "final_price": "9 995",
        "sizes": [{"sizeName": "S", "stock": 1}, {"sizeName": "M", "stock": 0}],
    }
    record = lpp_record(item, SOURCES["reserved"], "https://www.reserved.com/hu/hu/noi/kabatok")
    parsed = normalize(record)
    assert parsed.price_amount == Decimal(9995)
    assert parsed.availability_status == "unknown"


def test_oversized_photo_is_refused_before_the_form_is_parsed(tmp_path) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    with api_context(database):
        big = SimpleUploadedFile("big.jpg", b"0" * (6 * 1024 * 1024), content_type="image/jpeg")
        response = Client().post("/api/v1/search/image", {"file": big})
    assert response.status_code == 413
    assert response.json()["detail"] == "image exceeds 5 MB"


def _png(width: int, height: int, *, noisy: bool = False) -> bytes:
    """A noisy image keeps its pixel data large; a flat one compresses to a few KB."""
    size = (width, height)
    if noisy:
        image = Image.frombytes("RGB", size, random.Random(0).randbytes(width * height * 3))
    else:
        image = Image.new("RGB", size, "navy")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _post_photo(content: bytes):
    photo = SimpleUploadedFile("photo.png", content, content_type="image/png")
    return Client().post("/api/v1/search/image", {"file": photo})


def test_photo_pixels_are_decoded_only_once_photo_search_can_run(tmp_path, monkeypatch) -> None:
    # A valid header with truncated pixel data only fails once the pixels are decoded.
    truncated = _png(64, 64, noisy=True)[:6000]
    with api_context(open_database(tmp_path / "catalog.sqlite")):
        unavailable = _post_photo(truncated)
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == "image search is not configured"

    monkeypatch.setenv("FASHION_FINDER_DEMO_MODE", "1")
    root = tmp_path / "demo"
    seed_demo(root)
    with api_context(open_database(root / "fashion_finder.sqlite"), root / "source-policies.json"):
        available = _post_photo(truncated)
    assert available.status_code == 422
    assert available.json()["detail"] == "cannot decode image"


def test_photo_larger_than_the_web_app_sends_is_refused_from_its_header(tmp_path) -> None:
    # The web app sends at most 1024 px on the long side; 2100 x 2100 is over 4 MP.
    with api_context(open_database(tmp_path / "catalog.sqlite")):
        response = _post_photo(_png(2100, 2100))
    assert response.status_code == 413
    assert response.json()["detail"] == "image dimensions are too large"


def test_a_busy_database_answers_503(tmp_path, monkeypatch) -> None:
    database = open_database(tmp_path / "catalog.sqlite")

    def locked(*args, **kwargs):
        raise OperationalError("database is locked")

    monkeypatch.setattr(views, "search_public_catalogue", locked)
    with api_context(database):
        response = Client().get("/api/v1/search")
    assert response.status_code == 503
    assert "busy" in response.json()["detail"]
    assert offer_rows(database) == []

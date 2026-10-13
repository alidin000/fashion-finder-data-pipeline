"""Contracts at the publication boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.db import connections
from django.test import Client
from django.test.utils import CaptureQueriesContext
from PIL import Image

from fashion_finder_data.api.views import api_context
from fashion_finder_data.catalogue.models import RetailerOffer as StoredOffer
from fashion_finder_data.schema import RetailerOffer
from fashion_finder_data.source_policy import SourcePolicy, load_policies
from fashion_finder_data.storage import open_database, upsert_offer


class ApiRequester:
    def __init__(self, path, policy_path=None):
        self.database = open_database(path)
        self.policy_path = policy_path
        self.client = Client()

    def __enter__(self):
        self.bound = api_context(self.database, self.policy_path)
        self.bound.__enter__()
        return self

    def __exit__(self, *_args):
        self.bound.__exit__(*_args)

    def get(self, path, params=None):
        return self.client.get(path, data=params)

    def post(self, path, files):
        name, content, media_type = files["file"]
        stream = BytesIO(content)
        stream.name = name
        stream.content_type = media_type
        return self.client.post(path, data={"file": stream})


def add_offer(
    database,
    sku: str,
    price: str,
    checked: datetime,
    source: str = "reserved",
    availability: str = "unknown",
) -> None:
    url = f"https://www.{source}.com/hu/hu/jacket-{sku}"
    upsert_offer(
        database,
        RetailerOffer(
            source_key=source,
            source_url=url,
            canonical_url=url,
            source_product_id=sku,
            title=f"Black jacket {sku}",
            category="jackets",
            price_amount=Decimal(price),
            currency="HUF",
            image_urls=[f"https://www.{source}.com/images/{sku}.jpg"],
            colour_names=["Black"],
            size_options=["M"],
            availability_status=availability,
            source_fetched_at=checked,
        ),
    )


def policy_file(tmp_path, *, image_display: bool = False):
    path = tmp_path / "policies.json"
    path.write_text(
        json.dumps(
            [
                {
                    "source_key": "reserved",
                    "approval_status": "approved",
                    "evidence_ref": "private approval record 1",
                    "allow_public_listing": True,
                    "allow_image_display": image_display,
                    "freshness_hours": 24,
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_no_policy_denies_publication_and_approved_policy_filters_results(tmp_path) -> None:
    db_path = tmp_path / "catalog.sqlite"
    engine = open_database(db_path)
    now = datetime.now(UTC)
    add_offer(engine, "low", "9995", now)
    add_offer(engine, "high", "27000", now)
    add_offer(engine, "old", "7000", now - timedelta(days=5))
    add_offer(engine, "other", "8000", now, "zara")
    add_offer(engine, "sold", "5000", now, availability="out_of_stock")
    upsert_offer(
        engine,
        RetailerOffer(
            source_key="reserved",
            source_url="https://unrelated.example/hu/hu/jacket",
            canonical_url="https://unrelated.example/hu/hu/jacket",
            source_product_id="wrong-host",
            title="Wrong host",
            price_amount=Decimal(1000),
            currency="HUF",
            source_fetched_at=now,
        ),
    )
    with ApiRequester(db_path) as client:
        assert client.get("/api/v1/search").json()["items"] == []
        assert client.get("/api/v1/offers/1").status_code == 404

    path = policy_file(tmp_path)
    with ApiRequester(db_path, path) as client:
        response = client.get("/api/v1/search", params={"max_price": "15000", "limit": 1})
        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["source_product_id"] for item in items] == ["low"]
        assert items[0]["price_amount"] == "9995"
        assert items[0]["image_urls"] == []
        assert items[0]["match_type"] == "style_inspiration"
        assert client.get("/api/v1/offers/1").status_code == 200
        assert client.get("/api/v1/offers/3").status_code == 404
        assert client.get("/api/v1/offers/4").status_code == 404
        assert client.get("/api/v1/offers/5").status_code == 404
        assert client.get("/api/v1/offers/6").status_code == 404


def test_cursor_pages_are_bounded_and_bound_to_filters(tmp_path) -> None:
    db_path = tmp_path / "catalog.sqlite"
    engine = open_database(db_path)
    for i in range(4):
        add_offer(engine, str(i), str(10000 + i), datetime.now(UTC))
    with ApiRequester(db_path, policy_file(tmp_path, image_display=True)) as client:
        first = client.get("/api/v1/search", params={"limit": 2}).json()
        second = client.get(
            "/api/v1/search", params={"limit": 2, "cursor": first["next_cursor"]}
        ).json()
        assert len(first["items"]) == len(second["items"]) == 2
        assert {item["id"] for item in first["items"]}.isdisjoint(
            {item["id"] for item in second["items"]}
        )
        assert second["next_cursor"] is None
        assert first["items"][0]["image_urls"]
        assert (
            client.get(
                "/api/v1/search",
                params={"limit": 2, "max_price": "10002", "cursor": first["next_cursor"]},
            ).status_code
            == 400
        )
        assert client.get("/api/v1/search", params={"cursor": "%%%"}).status_code == 400


def test_policy_revocation_takes_effect_without_restart(tmp_path) -> None:
    db_path = tmp_path / "catalog.sqlite"
    add_offer(open_database(db_path), "x", "10000", datetime.now(UTC))
    path = policy_file(tmp_path)
    with ApiRequester(db_path, path) as client:
        assert len(client.get("/api/v1/search").json()["items"]) == 1
        path.write_text("[]", encoding="utf-8")
        assert client.get("/api/v1/search").json()["items"] == []


def test_public_images_include_only_safe_https_urls(tmp_path) -> None:
    db_path = tmp_path / "catalog.sqlite"
    database = open_database(db_path)
    add_offer(database, "safe-image", "10000", datetime.now(UTC))
    StoredOffer.objects.using(database.alias).update(
        image_urls=json.dumps(
            [
                "http://images.reserved.com/old.jpg",
                "javascript:alert(1)",
                "https://[",
                "https://images.reserved.com/safe.jpg",
            ]
        )
    )
    with ApiRequester(db_path, policy_file(tmp_path, image_display=True)) as client:
        response = client.get("/api/v1/search")
    assert response.status_code == 200
    assert response.json()["items"][0]["image_urls"] == ["https://images.reserved.com/safe.jpg"]


def test_unapproved_policy_cannot_grant_rights(tmp_path) -> None:
    with pytest.raises(ValueError, match="unapproved"):
        SourcePolicy(source_key="reserved", allow_public_listing=True)
    path = tmp_path / "bad.json"
    path.write_text('[{"source_key":"reserved"},{"source_key":"reserved"}]', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_policies(path)


def test_image_endpoint_validates_upload_then_reports_unavailable(tmp_path) -> None:
    with ApiRequester(tmp_path / "catalog.sqlite") as client:
        assert (
            client.post(
                "/api/v1/search/image", files={"file": ("bad.txt", b"hello", "text/plain")}
            ).status_code
            == 415
        )
        assert (
            client.post(
                "/api/v1/search/image", files={"file": ("bad.png", b"broken", "image/png")}
            ).status_code
            == 422
        )
        output = BytesIO()
        Image.new("RGB", (8, 8)).save(output, format="PNG")
        response = client.post(
            "/api/v1/search/image", files={"file": ("valid.png", output.getvalue(), "image/png")}
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "image search is not configured"


def test_local_web_origin_preflight_is_explicit_and_other_origins_are_denied(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FASHION_FINDER_CORS_ORIGINS", "http://localhost:3000")
    with ApiRequester(tmp_path / "catalog.sqlite") as client:
        allowed = client.client.options("/api/v1/search/image", HTTP_ORIGIN="http://localhost:3000")
        denied = client.client.options(
            "/api/v1/search/image", HTTP_ORIGIN="https://unrelated.example"
        )
    assert allowed.status_code == 204
    assert allowed["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert "Access-Control-Allow-Origin" not in denied


def test_public_queries_filter_approved_source_and_category_in_sql(tmp_path) -> None:
    db_path = tmp_path / "catalog.sqlite"
    database = open_database(db_path)
    now = datetime.now(UTC)
    add_offer(database, "approved", "10000", now)
    add_offer(database, "unapproved", "8000", now, source="zara")
    path = policy_file(tmp_path)
    with ApiRequester(db_path, path) as client:
        with CaptureQueriesContext(connections[database.alias]) as queries:
            response = client.get(
                "/api/v1/search", params={"source": "reserved", "category": "jackets"}
            )
        assert [item["source_product_id"] for item in response.json()["items"]] == ["approved"]
        offer_queries = [
            query["sql"] for query in queries if 'FROM "retailer_offer"' in query["sql"]
        ]
        assert len(offer_queries) == 1
        assert '"retailer"."source_key"' in offer_queries[0]
        assert '"retailer_offer"."category"' in offer_queries[0]
        with CaptureQueriesContext(connections[database.alias]) as denied_queries:
            denied = client.get("/api/v1/search", params={"source": "zara"})
        assert denied.json()["items"] == []
        assert not any('FROM "retailer_offer"' in query["sql"] for query in denied_queries)


def test_english_colour_and_single_size_match_hungarian_listing(tmp_path) -> None:
    db_path = tmp_path / "catalog.sqlite"
    database = open_database(db_path)
    url = "https://www.reserved.com/hu/hu/kabat-navy-123ab-45c"
    upsert_offer(
        database,
        RetailerOffer(
            source_key="reserved",
            source_url=url,
            canonical_url=url,
            source_product_id="navy",
            title="Gyapjúkabát",
            price_amount=Decimal(42995),
            currency="HUF",
            colour_names=["sötétkék"],
            size_options=["XS/S", "M/L"],
            source_fetched_at=datetime.now(UTC),
        ),
    )

    def found(params: dict) -> list[str]:
        items = client.get("/api/v1/search", params=params).json()["items"]
        return [item["source_product_id"] for item in items]

    with ApiRequester(db_path, policy_file(tmp_path)) as client:
        assert found({"colour": "blue", "size": "M"}) == ["navy"]
        assert found({"colour": "sotetkek", "q": "coat"}) == ["navy"]
        assert found({"colour": "black"}) == []
        assert found({"size": "XL"}) == []


def test_bad_legacy_offer_is_hidden_without_breaking_other_results(tmp_path) -> None:
    db_path = tmp_path / "catalog.sqlite"
    database = open_database(db_path)
    now = datetime.now(UTC)
    add_offer(database, "good", "10000", now)
    add_offer(database, "bad-price", "8000", now)
    add_offer(database, "bad-time", "7000", now)
    add_offer(database, "bad-colours", "6000", now)
    StoredOffer.objects.using(database.alias).filter(source_product_id="bad-price").update(
        price_amount="NaN"
    )
    StoredOffer.objects.using(database.alias).filter(source_product_id="bad-time").update(
        source_fetched_at="not-a-date"
    )
    StoredOffer.objects.using(database.alias).filter(source_product_id="bad-colours").update(
        colour_names="{"
    )
    with ApiRequester(db_path, policy_file(tmp_path)) as client:
        response = client.get("/api/v1/search")
        assert response.status_code == 200
        assert [item["source_product_id"] for item in response.json()["items"]] == ["good"]
        bad_id = StoredOffer.objects.using(database.alias).get(source_product_id="bad-time").id
        assert client.get(f"/api/v1/offers/{bad_id}").status_code == 404

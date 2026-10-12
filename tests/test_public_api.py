"""Contracts at the publication boundary."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO

import httpx
import pytest
from PIL import Image

from fashion_finder_data.api.app import create_app
from fashion_finder_data.schema import RetailerOffer
from fashion_finder_data.source_policy import SourcePolicy, load_policies
from fashion_finder_data.storage import open_database, upsert_offer


class ApiRequester:
    """Run ASGI requests without Starlette's thread-based portal."""

    def __init__(self, app):
        self.app = app

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def request(self, method: str, path: str, **kwargs):
        async def call():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://test"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(call())

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)


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
    with ApiRequester(create_app(db_path)) as client:
        assert client.get("/api/v1/search").json()["items"] == []
        assert client.get("/api/v1/offers/1").status_code == 404

    path = policy_file(tmp_path)
    with ApiRequester(create_app(db_path, path)) as client:
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
    with ApiRequester(create_app(db_path, policy_file(tmp_path, image_display=True))) as client:
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
    with ApiRequester(create_app(db_path, path)) as client:
        assert len(client.get("/api/v1/search").json()["items"]) == 1
        path.write_text("[]", encoding="utf-8")
        assert client.get("/api/v1/search").json()["items"] == []


def test_unapproved_policy_cannot_grant_rights(tmp_path) -> None:
    with pytest.raises(ValueError, match="unapproved"):
        SourcePolicy(source_key="reserved", allow_public_listing=True)
    path = tmp_path / "bad.json"
    path.write_text('[{"source_key":"reserved"},{"source_key":"reserved"}]', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_policies(path)


def test_image_endpoint_validates_upload_then_reports_unavailable(tmp_path) -> None:
    with ApiRequester(create_app(tmp_path / "catalog.sqlite")) as client:
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

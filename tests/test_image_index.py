"""Offline product-image indexing never bypasses source policy or current offers."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image

from fashion_finder_data.api.views import api_context
from fashion_finder_data.demo_images import demo_image_bytes
from fashion_finder_data.image_index import (
    INDEX_FILE,
    build_image_index,
    image_index_ready,
    search_indexed_image,
)
from fashion_finder_data.schema import RetailerOffer, utc_now
from fashion_finder_data.source_policy import SourcePolicy
from fashion_finder_data.storage import offer_rows, open_database, upsert_offer


def _catalogue(tmp_path):
    database = open_database(tmp_path / "fashion_finder.sqlite")
    images = [
        ("dress", "dresses", "Red", "demo-008"),
        ("jacket", "jackets", "Brown", "demo-003"),
    ]
    manifest = []
    for sku, category, colour, sample in images:
        product_url = f"https://www.reserved.com/hu/hu/{sku}-sample"
        image_url = f"https://www.reserved.com/hu/hu/images/{sku}.jpg"
        upsert_offer(
            database,
            RetailerOffer(
                source_key="reserved",
                source_url=product_url,
                canonical_url=product_url,
                source_product_id=sku,
                title=f"Sample {sku}",
                category=category,
                price_amount=Decimal(12000),
                currency="HUF",
                image_urls=[image_url],
                colour_names=[colour],
                size_options=["M"],
                source_fetched_at=utc_now(),
            ),
        )
        filename = f"{sku}.jpg"
        (tmp_path / filename).write_bytes(demo_image_bytes(sample))
        offer_id = next(
            row["id"] for row in offer_rows(database) if row["source_product_id"] == sku
        )
        manifest.append({"offer_id": offer_id, "image_url": image_url, "image_path": filename})
    path = tmp_path / "images.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    policy = SourcePolicy(
        source_key="reserved",
        approval_status="approved",
        evidence_ref="local test fixture only",
        allow_public_listing=True,
        allow_image_display=True,
        allow_image_processing=True,
        policy_version="test-1",
    )
    return database, path, {("reserved", "HU"): policy}, manifest


def test_offline_index_ranks_and_filters_only_current_permitted_offers(tmp_path, monkeypatch):
    database, manifest_path, policies, _manifest = _catalogue(tmp_path)
    report = build_image_index(database, policies, manifest_path)
    assert report["indexed_images"] == 2
    assert report["retailer_rights_verified_by_tool"] is False
    assert image_index_ready(database, policies)

    photo = demo_image_bytes("demo-008")
    assert photo is not None
    with Image.open(BytesIO(photo)) as image:
        assert (
            search_indexed_image(database, policies, image)["items"][0]["title"] == "Sample dress"
        )
        filtered = search_indexed_image(database, policies, image, category="jackets")
        assert [item["title"] for item in filtered["items"]] == ["Sample jacket"]
        assert search_indexed_image(database, policies, image, max_price=Decimal(10000)) == {
            "items": [],
            "next_cursor": None,
        }

    policy_path = tmp_path / "source-policies.json"
    policy_path.write_text(
        json.dumps([policies[("reserved", "HU")].model_dump()]), encoding="utf-8"
    )
    monkeypatch.setenv("FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT", "1")
    with api_context(database, policy_path):
        client = Client()
        assert client.get("/api/v1/health").json()["image_search"] == "available"
        response = client.post(
            "/api/v1/search/image",
            {
                "file": SimpleUploadedFile("dress.jpg", photo, content_type="image/jpeg"),
                "source": "reserved",
                "category": "dresses",
            },
        )
        assert response.status_code == 200
        assert [item["title"] for item in response.json()["items"]] == ["Sample dress"]

    monkeypatch.delenv("FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT")
    with api_context(database, policy_path):
        assert Client().get("/api/v1/health").json()["image_search"] == "unavailable"
        assert (
            Client()
            .post(
                "/api/v1/search/image",
                {"file": SimpleUploadedFile("dress.jpg", photo, content_type="image/jpeg")},
            )
            .status_code
            == 503
        )

    revoked = {
        ("reserved", "HU"): policies[("reserved", "HU")].model_copy(
            update={
                "approval_status": "revoked",
                "allow_public_listing": False,
                "allow_image_display": False,
                "allow_image_processing": False,
            }
        )
    }
    assert not image_index_ready(database, revoked)
    with Image.open(BytesIO(photo)) as image:
        assert search_indexed_image(database, revoked, image)["items"] == []

    # The index must stop using a source image when its catalogue URL changes.
    row = next(row for row in offer_rows(database) if row["source_product_id"] == "dress")
    from fashion_finder_data.catalogue.models import RetailerOffer as StoredOffer

    StoredOffer.objects.using(database.alias).filter(pk=row["id"]).update(
        image_urls=json.dumps(["https://www.reserved.com/hu/hu/images/new-dress.jpg"])
    )
    with Image.open(BytesIO(photo)) as image:
        assert [
            item["title"] for item in search_indexed_image(database, policies, image)["items"]
        ] == ["Sample jacket"]


def test_invalid_manifest_does_not_replace_a_working_index(tmp_path):
    database, manifest_path, policies, manifest = _catalogue(tmp_path)
    build_image_index(database, policies, manifest_path)
    target = tmp_path / INDEX_FILE
    initial_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    manifest[0]["image_path"] = "../outside.jpg"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="outside the manifest directory"):
        build_image_index(database, policies, manifest_path)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == initial_hash

    manifest[0]["image_path"] = "dress.jpg"
    denied = {
        ("reserved", "HU"): policies[("reserved", "HU")].model_copy(
            update={"allow_image_processing": False}
        )
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="does not permit images"):
        build_image_index(database, denied, manifest_path)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == initial_hash


def test_model_change_cannot_reuse_an_incompatible_image_index(tmp_path):
    database, manifest_path, policies, _manifest = _catalogue(tmp_path)
    build_image_index(database, policies, manifest_path)
    target = tmp_path / INDEX_FILE
    initial_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    missing_model = tmp_path / "missing.onnx"
    assert not image_index_ready(database, policies, model_path=missing_model)

    wrong_model = tmp_path / "wrong.onnx"
    wrong_model.write_bytes(b"not the pinned image model")
    with pytest.raises(ValueError, match="pinned image model"):
        build_image_index(database, policies, manifest_path, model_path=wrong_model)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == initial_hash

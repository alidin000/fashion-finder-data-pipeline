"""The photo-index ingest routes: token-only, checked per entry, merged, then searchable."""

from __future__ import annotations

import json
import math
import threading
from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image

from fashion_finder_data import image_index
from fashion_finder_data.api import ingest
from fashion_finder_data.api.views import api_context
from fashion_finder_data.demo_images import demo_image_bytes
from fashion_finder_data.hosted_index import vector_problem
from fashion_finder_data.image_index import (
    INDEX_FILE,
    merge_index_entries,
    pack_features,
    unpack_features,
)
from fashion_finder_data.schema import RetailerOffer, utc_now
from fashion_finder_data.storage import offer_rows, open_database, upsert_offer
from fashion_finder_data.visual_encoder import get_encoder

TOKEN = "t" * 40
AUTH = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}
COAT_PHOTO = "https://static.reserved.com/media/coat-front.jpg"
DRESS_PHOTO = "https://static.reserved.com/media/dress-front.jpg"
JACKET_PHOTO = "https://static.sinsay.com/media/jacket-front.jpg"


def policy(source_key: str, *, processing: bool = True, version: str = "photos-1") -> dict:
    return {
        "source_key": source_key,
        "approval_status": "approved",
        "evidence_ref": "test fixture",
        "allow_public_listing": True,
        "allow_image_display": True,
        "allow_image_processing": processing,
        "policy_version": version,
    }


def add_offer(database, source_key: str, sku: str, images: list[str], **extra) -> str:
    host = {"reserved": "www.reserved.com", "sinsay": "www.sinsay.com"}[source_key]
    url = f"https://{host}/hu/hu/termek-{sku.lower()}"
    upsert_offer(
        database,
        RetailerOffer(
            source_key=source_key,
            source_url=url,
            canonical_url=url,
            source_product_id=sku,
            title=f"Termék {sku}",
            category=extra.pop("category", "jackets"),
            price_amount=Decimal(19995),
            currency="HUF",
            image_urls=images,
            source_fetched_at=utc_now(),
            **extra,
        ),
    )
    return url


def vector(sample: str) -> str:
    with Image.open(BytesIO(demo_image_bytes(sample))) as image:
        return pack_features(get_encoder(None).encode(image))


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    """A catalogue like the hosted demo's, with the colour-layout encoder (no model file)."""
    monkeypatch.setenv(ingest.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT", "1")
    monkeypatch.delenv("FASHION_FINDER_IMAGE_MODEL_PATH", raising=False)
    monkeypatch.delenv("FASHION_FINDER_DEMO_MODE", raising=False)
    ingest._calls.clear()
    database = open_database(tmp_path / "fashion_finder.sqlite")
    policies = tmp_path / "source-policies.json"
    policies.write_text(
        json.dumps([policy("reserved"), policy("sinsay", processing=False)]), encoding="utf-8"
    )
    offers = {
        "coat": add_offer(database, "reserved", "C1", [COAT_PHOTO]),
        "dress": add_offer(database, "reserved", "D1", [DRESS_PHOTO], category="dresses"),
        "sold": add_offer(
            database,
            "reserved",
            "S1",
            ["https://static.reserved.com/media/sold.jpg"],
            availability_status="out_of_stock",
        ),
        "jacket": add_offer(database, "sinsay", "J1", [JACKET_PHOTO]),
    }
    with api_context(database, policies):
        yield database, policies, offers, Client(enforce_csrf_checks=True)


def entry(offer_url: str, image_url: str, sample: str = "demo-003", **changes) -> dict:
    return {
        "source_key": "reserved",
        "offer_url": offer_url,
        "image_url": image_url,
        "sha256": "a" * 64,
        "features": vector(sample),
        **changes,
    }


def upload(client: Client, entries: list, *, token: str = TOKEN, version: str | None = None):
    body = {
        "feature_version": version or get_encoder(None).version,
        "entries": entries,
    }
    return client.post(
        "/api/v1/ingest/image-index",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )


def stored(database) -> dict:
    return json.loads((database.path.parent / INDEX_FILE).read_text(encoding="utf-8"))


def test_photo_routes_need_the_configured_ingest_token(hosted, monkeypatch) -> None:
    database, _policies, offers, client = hosted
    good = [entry(offers["coat"], COAT_PHOTO)]
    assert upload(client, good, token="x" * 40).status_code == 401
    assert client.get("/api/v1/ingest/photos?source=reserved").status_code == 401
    for value in ("", "too-short"):
        monkeypatch.setenv(ingest.TOKEN_ENV, value)
        assert upload(client, good, token=value or "x").status_code == 503
        assert client.get("/api/v1/ingest/photos?source=reserved", **AUTH).status_code == 503
    assert not (database.path.parent / INDEX_FILE).exists()


def test_embeddings_from_another_model_are_refused_naming_both_versions(hosted) -> None:
    database, _policies, offers, client = hosted
    response = upload(client, [entry(offers["coat"], COAT_PHOTO)], version="vit-other:abc")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "vit-other:abc" in detail and get_encoder(None).version in detail
    assert not (database.path.parent / INDEX_FILE).exists()


def test_each_entry_is_checked_against_catalogue_policy_and_encoder(hosted, caplog) -> None:
    database, _policies, offers, client = hosted
    caplog.set_level("INFO", logger="fashion_finder_data.ingest.audit")
    length = len(unpack_features(vector("demo-003")))
    entries = [
        entry(offers["coat"], COAT_PHOTO),
        entry("https://www.reserved.com/hu/hu/unknown", COAT_PHOTO),
        entry(offers["dress"], "https://static.reserved.com/media/not-on-the-offer.jpg"),
        entry(offers["jacket"], JACKET_PHOTO, source_key="sinsay"),
        entry(offers["sold"], "https://static.reserved.com/media/sold.jpg"),
        entry(offers["dress"], DRESS_PHOTO, features=pack_features([0.5] * (length - 1))),
        entry(offers["dress"], DRESS_PHOTO, features=pack_features([math.nan] * length)),
        entry(offers["dress"], DRESS_PHOTO, features="not base64!"),
        entry("https://fashion-finder-demo.invalid/products/d-1", DRESS_PHOTO, source_key="demo"),
        entry("https://practice.invalid/products/p-1", DRESS_PHOTO, source_key="practice"),
        entry(offers["coat"], COAT_PHOTO),
    ]
    response = upload(client, entries)
    assert response.status_code == 200
    answer = response.json()
    reasons = [result.get("reason") for result in answer["results"]]
    assert answer["results"][0]["status"] == "indexed"
    assert answer["counts"] == {"indexed": 1, "rejected": 10}
    assert reasons[1] == "unknown offer for this source"
    assert reasons[2] == "photo URL is no longer on the offer"
    assert reasons[3] == "source policy does not permit images"
    assert reasons[4].startswith("offer is not currently visible")
    assert reasons[5] == f"vector has {length - 1} values; this encoder makes {length}"
    assert reasons[6] == "vector has non-finite values"
    assert reasons[7] == "features must be base64 of little-endian float32 values"
    assert reasons[8] == "demo is not a retailer source"
    assert reasons[9] == "practice is not a retailer source"
    assert reasons[10] == "duplicate photo in this upload"
    index = stored(database)
    assert [item["image_url"] for item in index["entries"]] == [COAT_PHOTO]
    [audit] = [r.getMessage() for r in caplog.records if r.name.endswith("ingest.audit")]
    assert audit.startswith("image index batch actor=ingest-token:") and TOKEN not in audit
    assert '"indexed": 1, "rejected": 10' in audit


def test_cosine_embeddings_must_be_unit_length() -> None:
    class Cosine:
        cosine = True
        length = 3

    assert vector_problem(pack_features([0.6, 0.8, 0.0]), Cosine()) is None
    assert vector_problem(pack_features([0.6, 0.8, 0.5]), Cosine()).startswith(
        "vector is not L2-normalised"
    )


def test_uploads_merge_in_build_format_and_search_finds_the_offer(hosted) -> None:
    database, policies, offers, client = hosted
    first = upload(client, [entry(offers["coat"], COAT_PHOTO, "demo-008")]).json()
    assert first["index_entries"] == 1
    upload(client, [entry(offers["dress"], DRESS_PHOTO, "demo-008", sha256="b" * 64)])
    # The coat's photo is uploaded again with a corrected embedding: replaced, not doubled.
    upload(client, [entry(offers["coat"], COAT_PHOTO, "demo-003", sha256="c" * 64)])

    index = stored(database)
    assert index["version"] == 2 and index["feature_version"] == get_encoder(None).version
    assert [(item["image_url"], item["sha256"]) for item in index["entries"]] == [
        (COAT_PHOTO, "c" * 64),
        (DRESS_PHOTO, "b" * 64),
    ]
    coat_id = next(row["id"] for row in offer_rows(database) if row["source_product_id"] == "C1")
    assert index["entries"][0] == {
        "offer_id": coat_id,
        "source_key": "reserved",
        "image_url": COAT_PHOTO,
        "policy_version": "photos-1",
        "sha256": "c" * 64,
        "features": vector("demo-003"),
    }

    assert client.get("/api/v1/health").json()["image_search"] == "available"
    response = client.post(
        "/api/v1/search/image",
        {
            "file": SimpleUploadedFile(
                "jacket.jpg", demo_image_bytes("demo-003"), content_type="image/jpeg"
            )
        },
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["title"] == "Termék C1"

    # A new policy version hides stale entries; re-uploading stores the current version.
    policies.write_text(
        json.dumps([policy("reserved", version="photos-2"), policy("sinsay", processing=False)]),
        encoding="utf-8",
    )
    upload(client, [entry(offers["coat"], COAT_PHOTO, "demo-003")])
    versions = {item["image_url"]: item["policy_version"] for item in stored(database)["entries"]}
    assert versions == {COAT_PHOTO: "photos-2", DRESS_PHOTO: "photos-1"}


def test_upload_size_and_batch_limits(hosted) -> None:
    _database, _policies, offers, client = hosted
    assert upload(client, [entry(offers["coat"], COAT_PHOTO)] * 501).status_code == 413
    huge = b'{"feature_version": "x", "entries": [], "pad": "' + b"x" * ingest.MAX_BODY_BYTES
    response = client.post(
        "/api/v1/ingest/image-index", data=huge, content_type="application/json", **AUTH
    )
    assert response.status_code == 413
    response = client.post(
        "/api/v1/ingest/image-index", data=b"[1, 2", content_type="application/json", **AUTH
    )
    assert response.status_code == 400
    response = client.post(
        "/api/v1/ingest/image-index", data=b"{}", content_type="text/plain", **AUTH
    )
    assert response.status_code == 415


def test_a_full_index_refuses_new_photos_but_accepts_replacements(hosted, monkeypatch) -> None:
    database, _policies, offers, client = hosted
    monkeypatch.setattr(image_index, "MAX_ENTRIES", 1)
    upload(client, [entry(offers["coat"], COAT_PHOTO)])
    answer = upload(
        client,
        [entry(offers["dress"], DRESS_PHOTO), entry(offers["coat"], COAT_PHOTO, sha256="d" * 64)],
    ).json()
    assert [result["status"] for result in answer["results"]] == ["rejected", "indexed"]
    assert answer["results"][0]["reason"].startswith("the photo index is full")
    assert [item["sha256"] for item in stored(database)["entries"]] == ["d" * 64]


def test_concurrent_uploads_never_lose_each_others_entries(tmp_path) -> None:
    database = open_database(tmp_path / "fashion_finder.sqlite")
    version = get_encoder(None).version
    features = vector("demo-003")

    def merge(number: int) -> None:
        for round_ in range(5):
            merge_index_entries(
                database,
                version,
                [
                    {
                        "offer_id": number * 100 + round_,
                        "source_key": "reserved",
                        "image_url": f"https://static.reserved.com/media/{number}-{round_}.jpg",
                        "policy_version": "1",
                        "sha256": "e" * 64,
                        "features": features,
                    }
                ],
            )

    threads = [threading.Thread(target=merge, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(stored(database)["entries"]) == 40


def test_photo_listing_shows_processable_offers_and_what_is_indexed(hosted) -> None:
    database, _policies, offers, client = hosted
    chart = "https://static.reserved.com/media/size-chart.jpg"
    second = add_offer(database, "reserved", "C2", [chart, "https://static.reserved.com/m/2.jpg"])
    upload(client, [entry(offers["coat"], COAT_PHOTO)])

    page = client.get("/api/v1/ingest/photos?source=reserved&per_offer=2", **AUTH).json()
    assert page["feature_version"] == get_encoder(None).version
    assert page["next_after"] is None
    listed = {offer["offer_url"]: offer for offer in page["offers"]}
    assert set(listed) == {offers["coat"], offers["dress"], second}  # not the sold-out offer
    assert listed[offers["coat"]]["indexed_image_urls"] == [COAT_PHOTO]
    assert listed[offers["dress"]]["indexed_image_urls"] == []
    assert listed[second]["image_urls"] == ["https://static.reserved.com/m/2.jpg"]  # no chart

    first = client.get("/api/v1/ingest/photos?source=reserved&limit=1", **AUTH).json()
    rest = client.get(
        f"/api/v1/ingest/photos?source=reserved&after={first['next_after']}", **AUTH
    ).json()
    assert [o["offer_url"] for o in first["offers"] + rest["offers"]] == [
        o["offer_url"] for o in page["offers"]
    ]
    sinsay = client.get("/api/v1/ingest/photos?source=sinsay", **AUTH).json()
    assert sinsay["offers"] == []  # listed and shown, but photo processing is not approved
    for query in ("source=demo", "source=reserved&per_offer=9", "source=reserved&limit=0"):
        assert client.get(f"/api/v1/ingest/photos?{query}", **AUTH).status_code == 400

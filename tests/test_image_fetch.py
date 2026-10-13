"""Photo fetching respects robots.txt and retailer domains, and feeds index-images."""

from __future__ import annotations

import urllib.error
from decimal import Decimal

from fashion_finder_data.demo_images import demo_image_bytes
from fashion_finder_data.image_fetch import fetch_images
from fashion_finder_data.image_index import (
    _distances,
    build_image_index,
    pack_features,
    unpack_features,
)
from fashion_finder_data.schema import RetailerOffer, utc_now
from fashion_finder_data.source_policy import SourcePolicy
from fashion_finder_data.storage import offer_rows, open_database, upsert_offer

ROBOTS = b"User-agent: *\nDisallow: /private/\n"
PHOTO = "https://static.reserved.com/media/coat-front.jpg"


def add_offer(database, sku: str, images: list[str]) -> int:
    url = f"https://www.reserved.com/hu/hu/coat-{sku}"
    upsert_offer(
        database,
        RetailerOffer(
            source_key="reserved",
            source_url=url,
            canonical_url=url,
            source_product_id=sku,
            title=f"Kabát {sku}",
            price_amount=Decimal(19995),
            currency="HUF",
            image_urls=images,
            source_fetched_at=utc_now(),
        ),
    )
    return next(row["id"] for row in offer_rows(database) if row["source_product_id"] == sku)


class FakeWeb:
    def __init__(self, image: bytes):
        self.image = image
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return ROBOTS
        if "missing" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return self.image


def test_fetch_downloads_allowed_photos_and_writes_an_indexable_manifest(tmp_path) -> None:
    database = open_database(tmp_path / "fashion_finder.sqlite")
    coat = add_offer(
        database,
        "C1",
        [PHOTO, "https://static.reserved.com/private/coat-back.jpg", "https://x.example/3.jpg"],
    )
    add_offer(database, "C2", ["https://cdn.example.net/other.jpg"])
    web = FakeWeb(demo_image_bytes("demo-003"))
    pauses: list[float] = []
    output = tmp_path / "images"

    report = fetch_images(database, output, per_offer=2, delay=3.0, fetch=web, sleep=pauses.append)

    assert report["downloaded"] == 1
    assert report["failures"] == {
        "disallowed or unreadable robots.txt": 1,
        "image URL is not on the retailer's HTTPS domains": 1,
    }
    assert web.calls == ["https://static.reserved.com/robots.txt", PHOTO]
    assert pauses == [3.0]  # one pause, between the robots.txt and photo requests
    manifest = (output / "manifest.json").read_text(encoding="utf-8")
    assert f'"offer_id": {coat}' in manifest
    assert len(list((output / "reserved").glob("*.jpg"))) == 1

    again = fetch_images(database, output, per_offer=2, fetch=web, sleep=pauses.append)
    assert again["downloaded"] == 0
    assert again["reused"] == 1
    assert web.calls[2:] == ["https://static.reserved.com/robots.txt"]  # rules only

    policy = SourcePolicy(
        source_key="reserved",
        approval_status="approved",
        evidence_ref="local test fixture only",
        allow_public_listing=True,
        allow_image_display=True,
        allow_image_processing=True,
    )
    indexed = build_image_index(database, {("reserved", "HU"): policy}, output / "manifest.json")
    assert indexed["indexed_images"] == 1


def test_fetch_records_bad_responses_without_writing_files(tmp_path) -> None:
    database = open_database(tmp_path / "fashion_finder.sqlite")
    add_offer(
        database,
        "C1",
        ["https://static.reserved.com/media/missing.jpg", "https://static.reserved.com/m/x.jpg"],
    )
    web = FakeWeb(b"<html>not an image</html>")
    report = fetch_images(database, tmp_path / "images", fetch=web, sleep=lambda _: None)
    assert report["status"] == "failed"
    assert report["failures"] == {"HTTP 404": 1, "cannot decode image": 1}
    assert not (tmp_path / "images" / "reserved").exists()


def test_matrix_ranking_matches_pairwise_cosine_distance() -> None:
    class Cosine:
        cosine = True
        length = 3

        def distance(self, query, indexed):
            return 1.0 - sum(a * b for a, b in zip(query, indexed))

    entries = [
        {"features": unpack_features(pack_features(values))}
        for values in ([1.0, 0.0, 0.0], [0.0, 0.6, 0.8], [0.6, 0.8, 0.0])
    ]
    query = [0.6, 0.8, 0.0]
    fast = _distances(Cosine(), query, entries)
    slow = [Cosine().distance(query, entry["features"]) for entry in entries]
    assert [round(value, 5) for value in fast] == [round(value, 5) for value in slow]
    assert min(range(3), key=fast.__getitem__) == 2

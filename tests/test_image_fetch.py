"""Photo fetching respects policy, robots.txt, TDM opt-outs and retailer domains."""

from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request
from decimal import Decimal

import pytest

from fashion_finder_data.catalogue.models import RetailerOffer as StoredOffer
from fashion_finder_data.demo_images import demo_image_bytes
from fashion_finder_data.image_fetch import _SameSiteRedirects, fetch_images
from fashion_finder_data.image_index import (
    _distances,
    _stack,
    build_image_index,
    pack_features,
    unpack_features,
)
from fashion_finder_data.schema import RetailerOffer, utc_now
from fashion_finder_data.source_policy import SourcePolicy
from fashion_finder_data.storage import offer_rows, open_database, upsert_offer

# The wildcard rule needs RFC 9309 parsing; Python 3.12's robotparser ignores it.
ROBOTS = b"User-agent: *\nDisallow: /private/\nDisallow: /*-back.jpg$\n"
PHOTO = "https://static.reserved.com/media/coat-front.jpg"
POLICIES = {
    ("reserved", "HU"): SourcePolicy(
        source_key="reserved",
        approval_status="approved",
        evidence_ref="local test fixture only",
        allow_public_listing=True,
        allow_image_display=True,
        allow_image_processing=True,
    )
}


def add_offer(database, sku: str, images: list[str], availability: str = "unknown") -> int:
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
            availability_status=availability,
            source_fetched_at=utc_now(),
        ),
    )
    return next(row["id"] for row in offer_rows(database) if row["source_product_id"] == sku)


class FakeClock:
    """Sleeping advances time, so per-host spacing can be checked exactly."""

    def __init__(self):
        self.now = 1000.0
        self.pauses: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


class FakeWeb:
    def __init__(
        self, image: bytes, tdmrep: dict[str, list] | None = None, headers=None, clock=None
    ):
        self.image = image
        self.tdmrep = tdmrep or {}
        self.headers = headers or {}
        self.calls: list[str] = []
        self.clock = clock
        self.sent: list[tuple[str, float]] = []
        self.request_headers: dict[str, dict] = {}

    def __call__(self, url: str, headers=None):
        self.calls.append(url)
        self.request_headers[url] = dict(headers or {})
        if self.clock is not None:
            self.sent.append((url, self.clock.now))
        if url.endswith("/robots.txt"):
            return ROBOTS, {}
        if url.endswith("/.well-known/tdmrep.json"):
            origin = url.removesuffix("/.well-known/tdmrep.json")
            if origin not in self.tdmrep:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return json.dumps(self.tdmrep[origin]).encode(), {}
        if "missing" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        answer = self.headers.get(url, {})
        if answer.get("status") == "304":
            raise urllib.error.HTTPError(url, 304, "Not Modified", {}, None)
        return answer.get("body", self.image), {k: v for k, v in answer.items() if k != "body"}


def test_fetch_writes_a_manifest_that_indexes_even_after_an_item_sells_out(tmp_path) -> None:
    database = open_database(tmp_path / "fashion_finder.sqlite")
    coat = add_offer(
        database,
        "C1",
        [
            PHOTO,
            "https://static.reserved.com/private/coat-side.jpg",
            "https://static.reserved.com/media/coat-back.jpg",
        ],
    )
    add_offer(database, "C2", ["https://cdn.example.net/other.jpg"])
    add_offer(database, "C3", ["https://static.reserved.com/media/sold.jpg"], "out_of_stock")
    later = add_offer(database, "C4", ["https://static.reserved.com/media/c4.jpg"])
    clock = FakeClock()
    web = FakeWeb(
        demo_image_bytes("demo-003"),
        headers={
            "https://static.reserved.com/media/c4.jpg": {"body": demo_image_bytes("demo-008")}
        },
        clock=clock,
    )
    output = tmp_path / "images"

    report = fetch_images(
        database,
        output,
        POLICIES,
        per_offer=3,
        delay=3.0,
        fetch=web,
        sleep=clock.sleep,
        clock=clock,
    )

    assert report["downloaded"] == 2
    assert report["failures"] == {
        "disallowed or unreadable robots.txt": 2,  # /private/ and the *-back.jpg$ rule
        "image URL is not on the retailer's HTTPS domains": 1,
    }
    assert list(report["skipped_not_eligible"].values()) == [1]  # the sold-out C3
    assert web.calls[:4] == [
        "https://static.reserved.com/robots.txt",
        "https://static.reserved.com/.well-known/tdmrep.json",
        "https://www.reserved.com/.well-known/tdmrep.json",
        PHOTO,
    ]
    per_host: dict[str, list[float]] = {}
    for url, at in web.sent:
        per_host.setdefault(url.split("/")[2], []).append(at)
    assert all(
        later - earlier >= 3.0
        for times in per_host.values()
        for earlier, later in itertools.pairwise(times)
    )
    assert f'"offer_id": {coat}' in (output / "manifest.json").read_text(encoding="utf-8")

    again = fetch_images(database, output, POLICIES, per_offer=3, fetch=web, sleep=lambda _: 0)
    assert (again["downloaded"], again["reused"]) == (0, 2)

    StoredOffer.objects.using(database.alias).filter(pk=later).update(
        availability_status="out_of_stock"
    )
    indexed = build_image_index(database, POLICIES, output / "manifest.json")
    assert (indexed["status"], indexed["indexed_images"]) == ("partial", 1)
    assert sum(indexed["skipped_images"].values()) == 1


def test_fetch_honours_text_and_data_mining_reservations(tmp_path) -> None:
    database = open_database(tmp_path / "fashion_finder.sqlite")
    add_offer(database, "C1", [PHOTO])
    reserved_site = FakeWeb(
        demo_image_bytes("demo-003"),
        tdmrep={"https://www.reserved.com": [{"location": "/", "tdm-reservation": 1}]},
    )
    report = fetch_images(
        database, tmp_path / "a", POLICIES, fetch=reserved_site, sleep=lambda _: 0
    )
    assert report["failures"] == {"text-and-data-mining reserved (TDMRep)": 1}
    assert PHOTO not in reserved_site.calls

    header = FakeWeb(demo_image_bytes("demo-003"), headers={PHOTO: {"tdm-reservation": "1"}})
    report = fetch_images(database, tmp_path / "b", POLICIES, fetch=header, sleep=lambda _: 0)
    assert report["failures"] == {"text-and-data-mining reserved (header)": 1}
    assert not (tmp_path / "b" / "reserved").exists()


def test_fetch_skips_sources_without_image_permission(tmp_path) -> None:
    database = open_database(tmp_path / "fashion_finder.sqlite")
    add_offer(database, "C1", [PHOTO])
    web = FakeWeb(demo_image_bytes("demo-003"))
    report = fetch_images(database, tmp_path / "images", {}, fetch=web, sleep=lambda _: 0)
    assert report["skipped_not_eligible"] == {"source policy does not permit images": 1}
    assert web.calls == []


def test_fetch_records_bad_responses_without_writing_files(tmp_path) -> None:
    database = open_database(tmp_path / "fashion_finder.sqlite")
    add_offer(
        database,
        "C1",
        ["https://static.reserved.com/media/missing.jpg", "https://static.reserved.com/m/x.jpg"],
    )
    web = FakeWeb(b"<html>not an image</html>")
    report = fetch_images(database, tmp_path / "images", POLICIES, fetch=web, sleep=lambda _: 0)
    assert report["status"] == "failed"
    assert report["failures"] == {"HTTP 404": 1, "cannot decode image": 1}
    assert not (tmp_path / "images" / "reserved").exists()


def test_redirects_may_not_leave_the_retailer_site() -> None:
    handler = _SameSiteRedirects()
    request = urllib.request.Request("https://static.reserved.com/media/a.jpg")
    moved = handler.redirect_request(
        request, None, 302, "Found", {}, "https://cdn.reserved.com/a.jpg"
    )
    assert moved.full_url == "https://cdn.reserved.com/a.jpg"
    for target in ("https://evil.example/a.jpg", "http://static.reserved.com/a.jpg"):
        with pytest.raises(urllib.error.HTTPError):
            handler.redirect_request(request, None, 302, "Found", {}, target)


def test_matrix_ranking_matches_pairwise_cosine_distance() -> None:
    class Cosine:
        cosine = True
        length = 3

        def distance(self, query, indexed):
            return 1.0 - sum(a * b for a, b in zip(query, indexed))

    entries = [
        {"features": unpack_features(pack_features(values)), "_row": row}
        for row, values in enumerate(([1.0, 0.0, 0.0], [0.0, 0.6, 0.8], [0.6, 0.8, 0.0]))
    ]
    query = [0.6, 0.8, 0.0]
    slow = [Cosine().distance(query, entry["features"]) for entry in entries]
    stacked = _distances(Cosine(), query, entries)
    cached = _distances(Cosine(), query, entries[1:], _stack(entries, 3))  # candidates subset
    assert [round(value, 5) for value in stacked] == [round(value, 5) for value in slow]
    assert [round(value, 5) for value in cached] == [round(value, 5) for value in slow[1:]]
    assert min(range(3), key=stacked.__getitem__) == 2


def test_image_host_crawl_delay_and_lpp_tdmrep_redirect_are_honoured(tmp_path) -> None:
    database = open_database(tmp_path / "fashion_finder.sqlite")
    add_offer(database, "C1", [PHOTO])
    add_offer(database, "C2", ["https://static.reserved.com/media/coat-two.jpg"])
    clock = FakeClock()

    class LppWeb(FakeWeb):
        def __call__(self, url: str, headers=None):
            if url.endswith("/robots.txt"):
                self.calls.append(url)
                self.sent.append((url, clock.now))
                return b"User-agent: *\nCrawl-delay: 10\n", {}
            if url.endswith("/.well-known/tdmrep.json"):
                self.calls.append(url)
                self.sent.append((url, clock.now))
                # Reserved, Mohito, House and Cropp answer 301 with no Location.
                raise urllib.error.HTTPError(url, 301, "Moved Permanently", {}, None)
            return super().__call__(url, headers)

    web = LppWeb(
        demo_image_bytes("demo-003"),
        headers={
            "https://static.reserved.com/media/coat-two.jpg": {"body": demo_image_bytes("demo-009")}
        },
        clock=clock,
    )
    report = fetch_images(
        database,
        tmp_path / "images",
        POLICIES,
        delay=2.0,
        fetch=web,
        sleep=clock.sleep,
        clock=clock,
    )
    assert report["downloaded"] == 2
    assert report["failures"] == {}
    assert report["crawl_delays"] == {"static.reserved.com": 10.0}
    static = [at for url, at in web.sent if "static.reserved.com" in url]
    assert min(later - earlier for earlier, later in itertools.pairwise(static)) >= 10.0


def test_best_photos_skip_size_charts_and_identical_bytes_are_stored_once(tmp_path) -> None:
    database = open_database(tmp_path / "fashion_finder.sqlite")
    chart = "https://static.reserved.com/media/coat-size-chart.jpg"
    first = add_offer(database, "C1", [chart, PHOTO])
    second = add_offer(database, "C2", ["https://static.reserved.com/media/same-photo.jpg"])
    web = FakeWeb(demo_image_bytes("demo-003"))
    report = fetch_images(
        database, tmp_path / "images", POLICIES, per_offer=1, fetch=web, sleep=lambda _: 0
    )
    assert chart not in web.calls  # the packshot, not the size chart, fills the one slot
    assert (report["downloaded"], report["deduplicated"]) == (1, 1)
    manifest = json.loads((tmp_path / "images" / "manifest.json").read_text(encoding="utf-8"))
    paths = {entry["offer_id"]: entry["image_path"] for entry in manifest}
    assert paths[first] == paths[second]


def test_revalidation_sends_validators_and_keeps_unchanged_files(tmp_path) -> None:
    database = open_database(tmp_path / "fashion_finder.sqlite")
    add_offer(database, "C1", [PHOTO])
    web = FakeWeb(demo_image_bytes("demo-003"), headers={PHOTO: {"etag": '"v1"'}})
    output = tmp_path / "images"
    fetch_images(database, output, POLICIES, fetch=web, sleep=lambda _: 0)
    written = next((output / "reserved").iterdir())
    stamp = written.stat().st_mtime_ns
    cache = json.loads((output / "image-cache.json").read_text(encoding="utf-8"))
    cache[PHOTO]["fetched_at"] = "2026-01-01T00:00:00+00:00"  # fetched long ago
    (output / "image-cache.json").write_text(json.dumps(cache), encoding="utf-8")
    web.headers[PHOTO] = {"status": "304"}
    again = fetch_images(
        database, output, POLICIES, fetch=web, sleep=lambda _: 0, revalidate_after_days=7
    )
    assert again["unchanged_on_revalidation"] == 1
    assert web.request_headers[PHOTO] == {"If-None-Match": '"v1"'}
    assert written.stat().st_mtime_ns == stamp


def test_ai_opt_out_header_blocks_a_photo(tmp_path) -> None:
    database = open_database(tmp_path / "fashion_finder.sqlite")
    add_offer(database, "C1", [PHOTO])
    web = FakeWeb(
        demo_image_bytes("demo-003"), headers={PHOTO: {"x-robots-tag": "noai, noimageai"}}
    )
    report = fetch_images(database, tmp_path / "images", POLICIES, fetch=web, sleep=lambda _: 0)
    assert report["failures"] == {"image opts out of AI use (X-Robots-Tag)": 1}

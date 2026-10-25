"""``index-photos`` against the real ingest views: polite downloads, a vector-only cache."""

from __future__ import annotations

import itertools
import json
import re
import urllib.error
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from fashion_finder_data import photo_runner
from fashion_finder_data.api import ingest
from fashion_finder_data.api.views import api_context
from fashion_finder_data.demo_images import demo_image_bytes
from fashion_finder_data.image_index import INDEX_FILE
from fashion_finder_data.photo_runner import (
    CACHE_FILE,
    PhotoIndexError,
    photo_markdown,
    run_index_photos,
)
from fashion_finder_data.runner import ApiBackend, ApiError
from fashion_finder_data.schema import RetailerOffer, utc_now
from fashion_finder_data.storage import open_database, upsert_offer
from fashion_finder_data.visual_encoder import get_encoder

TOKEN = "t" * 40
COAT = "https://static.reserved.com/media/coat-front.jpg"
DRESS = "https://static.reserved.com/media/dress-front.jpg"
PRIVATE = "https://static.reserved.com/private/hidden.jpg"
SINSAY = "https://static.sinsay.com/media/top-front.jpg"
ROBOTS = b"User-agent: *\nDisallow: /private/\nCrawl-delay: 10\n"


class FakeClock:
    """Sleeping advances time, so spacing and the time budget can be checked exactly."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeShops:
    """The shops' image hosts: robots.txt, TDMRep files and photos."""

    def __init__(self, clock: FakeClock, tdmrep: dict[str, list] | None = None):
        self.clock = clock
        self.tdmrep = tdmrep or {}
        self.photos = {COAT: "demo-003", DRESS: "demo-008", PRIVATE: "demo-009", SINSAY: "demo-011"}
        self.answers: dict[str, dict] = {}
        self.sent: list[tuple[str, float, dict]] = []

    def __call__(self, url: str, headers=None):
        self.sent.append((url, self.clock.now, dict(headers or {})))
        if url.endswith("/robots.txt"):
            return ROBOTS, {}
        if url.endswith("/.well-known/tdmrep.json"):
            origin = url.removesuffix("/.well-known/tdmrep.json")
            if origin not in self.tdmrep:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return json.dumps(self.tdmrep[origin]).encode(), {}
        answer = self.answers.get(url, {})
        if answer.get("status") == 304:
            raise urllib.error.HTTPError(url, 304, "Not Modified", {}, None)
        return demo_image_bytes(self.photos[url]), {"etag": f'"{url[-12:]}"'}

    def photo_requests(self) -> list[str]:
        return [url for url, _, _ in self.sent if url.endswith(".jpg")]


class ClientBackend(ApiBackend):
    """The real ``ApiBackend`` methods, answered in-process by the Django ingest views."""

    def __init__(self, client: Client):
        super().__init__("http://127.0.0.1:8301", TOKEN)
        self.client = client
        self.calls: list[tuple[str, str]] = []

    def _call(self, method, path, *, params=None, body=None):
        self.calls.append((method, path))
        auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}
        if method == "GET":
            response = self.client.get(path, params or {}, **auth)
        else:
            response = self.client.post(
                path, data=json.dumps(body), content_type="application/json", **auth
            )
        if response.status_code != 200:
            raise ApiError(f"{method} {path}: HTTP {response.status_code} {response.content!r}")
        return response.json()


def add_offer(database, source_key: str, sku: str, images: list[str]) -> None:
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
            price_amount=Decimal(19995),
            currency="HUF",
            image_urls=images,
            source_fetched_at=utc_now(),
        ),
    )


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv(ingest.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT", "1")
    monkeypatch.delenv("FASHION_FINDER_IMAGE_MODEL_PATH", raising=False)
    monkeypatch.delenv("FASHION_FINDER_DEMO_MODE", raising=False)
    ingest._calls.clear()
    database = open_database(tmp_path / "api" / "fashion_finder.sqlite")
    policies = tmp_path / "source-policies.json"
    policies.write_text(
        json.dumps(
            [
                {
                    "source_key": key,
                    "approval_status": "approved",
                    "evidence_ref": "test fixture",
                    "allow_public_listing": True,
                    "allow_image_display": True,
                    "allow_image_processing": True,
                    "policy_version": "photos-1",
                }
                for key in ("reserved", "sinsay")
            ]
        ),
        encoding="utf-8",
    )
    add_offer(database, "reserved", "C1", [COAT])
    add_offer(database, "reserved", "D1", [DRESS])
    add_offer(database, "reserved", "P1", [PRIVATE])
    add_offer(database, "sinsay", "T1", [SINSAY])
    with api_context(database, policies):
        client = Client()
        yield database, client, ClientBackend(client)


def run(backend, cache_dir, shops: FakeShops, clock: FakeClock, **options) -> dict:
    return run_index_photos(
        backend,
        ["reserved", "sinsay"],
        cache_dir=cache_dir,
        fetch=shops,
        sleep=clock.sleep,
        clock=clock,
        **options,
    )


def test_photos_are_fetched_politely_uploaded_and_found_by_photo_search(api, tmp_path) -> None:
    _database, client, backend = api
    clock = FakeClock()
    sinsay_reserved = [{"location": "/", "tdm-reservation": 1}]
    shops = FakeShops(clock, tdmrep={"https://static.sinsay.com": sinsay_reserved})
    cache = tmp_path / "cache"

    report = run(backend, cache, shops, clock)

    assert (report["offers"], report["downloaded"], report["uploaded"]) == (4, 2, 2)
    assert report["skipped_reasons"] == {
        "disallowed or unreadable robots.txt": 1,
        "text-and-data-mining reserved (TDMRep)": 1,
    }
    assert PRIVATE not in shops.photo_requests() and SINSAY not in shops.photo_requests()
    # robots.txt asks for 10 s between requests to the image host.
    assert report["crawl_delays"] == {"static.reserved.com": 10.0, "static.sinsay.com": 10.0}
    times = [at for url, at, _ in shops.sent if "static.reserved.com" in url]
    assert min(later - earlier for earlier, later in itertools.pairwise(times)) >= 10.0
    # Vectors only: one JSON file, without photo bytes.
    assert [path.name for path in cache.iterdir()] == [CACHE_FILE]
    cached = json.loads((cache / CACHE_FILE).read_text(encoding="utf-8"))["photos"]
    assert set(cached) == {COAT, DRESS}
    assert all(
        set(record)
        == {"sha256", "etag", "last_modified", "feature_version", "fetched_at", "features"}
        for record in cached.values()
    )

    response = client.post(
        "/api/v1/search/image",
        {"file": SimpleUploadedFile("q.jpg", demo_image_bytes("demo-008"), "image/jpeg")},
    )
    assert response.json()["items"][0]["title"] == "Termék D1"

    photos_before = shops.photo_requests()
    again = run(backend, cache, shops, clock)
    assert (again["already_indexed"], again["uploaded"], again["image_requests"]) == (2, 0, 0)
    assert shops.photo_requests() == photos_before  # refusals are re-checked, photos never


def test_after_a_wipe_the_cache_rebuilds_the_index_without_photo_requests(api, tmp_path) -> None:
    database, _client, backend = api
    clock = FakeClock()
    shops = FakeShops(clock)
    cache = tmp_path / "cache"
    run(backend, cache, shops, clock)
    photos_before = shops.photo_requests()
    (database.path.parent / INDEX_FILE).unlink()  # the free instance slept and lost its files

    report = run(backend, cache, shops, clock)

    # Only robots.txt is read again, for the disallowed photo; no photo is downloaded.
    assert shops.photo_requests() == photos_before
    assert [url for url, _, _ in shops.sent[-1:]] == ["https://static.reserved.com/robots.txt"]
    assert (report["from_cache"], report["uploaded"], report["image_requests"]) == (3, 3, 0)
    assert len(json.loads((database.path.parent / INDEX_FILE).read_text())["entries"]) == 3


def test_an_old_cached_photo_is_revalidated_and_304_keeps_its_vector(api, tmp_path) -> None:
    database, _client, backend = api
    clock = FakeClock()
    shops = FakeShops(clock)
    cache = tmp_path / "cache"
    run(backend, cache, shops, clock)
    stored = json.loads((cache / CACHE_FILE).read_text(encoding="utf-8"))
    vector = stored["photos"][COAT]["features"]
    stored["photos"][COAT]["fetched_at"] = "2026-01-01T00:00:00+00:00"
    (cache / CACHE_FILE).write_text(json.dumps(stored), encoding="utf-8")
    (database.path.parent / INDEX_FILE).unlink()
    shops.answers[COAT] = {"status": 304}
    shops.sent.clear()

    report = run(backend, cache, shops, clock, revalidate_days=14)

    assert shops.photo_requests() == [COAT]  # only the old one; the others come from the cache
    [(_, _, headers)] = [sent for sent in shops.sent if sent[0] == COAT]
    assert headers == {"If-None-Match": f'"{COAT[-12:]}"'}
    assert (report["revalidated_unchanged"], report["from_cache"], report["uploaded"]) == (1, 2, 3)
    refreshed = json.loads((cache / CACHE_FILE).read_text(encoding="utf-8"))["photos"][COAT]
    assert refreshed["features"] == vector and refreshed["fetched_at"] > "2026-01-01"


def test_the_time_budget_stops_cleanly_and_keeps_finished_work(api, tmp_path) -> None:
    database, _client, backend = api
    for number in range(10):
        url = f"https://static.reserved.com/media/extra-{number}.jpg"
        add_offer(database, "reserved", f"X{number}", [url])
    clock = FakeClock()
    shops = FakeShops(clock)
    shops.photos.update(
        {f"https://static.reserved.com/media/extra-{n}.jpg": "demo-003" for n in range(10)}
    )
    cache = tmp_path / "cache"

    report = run(backend, cache, shops, clock, time_budget_minutes=1)

    # Crawl-delay 10 s on each image host: robots.txt and TDMRep take the first 20 s, then
    # the coat (20 s), Sinsay's photo (40 s), the dress (40 s) and extra-0 (50 s); extra-1
    # would start at 60 s, so the run stops there.
    assert report["stopped"] == "time budget of 1 minutes reached"
    assert (report["downloaded"], report["uploaded"], report["not_attempted"]) == (4, 4, 9)
    assert all(at - 1000.0 < 60 for url, at, _ in shops.sent)
    cached = json.loads((cache / CACHE_FILE).read_text(encoding="utf-8"))["photos"]
    assert len(cached) == 4
    summary = photo_markdown(report, "Fashion Finder photo index")
    assert "| reserved | 13 | 0 | 0 | 3 | 0 | 3 | 0 | 1 | 0 |" in summary
    assert "9 photos are left for the next run" in summary


def test_a_run_with_another_model_stops_before_any_download(api, tmp_path, monkeypatch) -> None:
    _database, _client, backend = api
    clock = FakeClock()
    shops = FakeShops(clock)

    class OtherModel:
        version = "marqo-fashion-siglip-vision-int8:other"

    monkeypatch.setattr(photo_runner, "get_encoder", lambda path: OtherModel())
    with pytest.raises(PhotoIndexError, match=re.escape(get_encoder(None).version)):
        run(backend, tmp_path / "cache", shops, clock)
    assert shops.sent == []

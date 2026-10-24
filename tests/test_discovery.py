"""Sitemap discovery, child rotation, pagination and refresh priorities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from scrapy.http import Request, Response, XmlResponse

from fashion_finder_data.collectors import CrawlState, ProductSpider
from fashion_finder_data.discovery import (
    PRIORITY_CHANGED_UPSTREAM,
    PRIORITY_NEW,
    choose_child_sitemaps,
    next_listing_page,
    parse_sitemap,
    prioritise,
)
from fashion_finder_data.sources import SOURCES

PAGES = Path(__file__).parent / "fixtures" / "pages"
NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def test_sitemap_index_and_gzip_urlset_with_lastmod() -> None:
    index = parse_sitemap(
        (PAGES / "sitemap-index.xml").read_bytes(), "https://sportano.hu/sitemap/"
    )
    assert [entry.is_sitemap for entry in index] == [True, True, True]
    assert index[1].lastmod == datetime(2026, 10, 23, 6, tzinfo=UTC)
    products = parse_sitemap((PAGES / "products_hu_1.xml.gz").read_bytes(), "https://sportano.hu/")
    assert [entry.url for entry in products][:2] == [
        "https://sportano.hu/futokabat-sp1",
        "https://sportano.hu/kotott-pulover-sp77",
    ]
    assert all("passwd" not in entry.url and "root:" not in entry.url for entry in products)


def test_child_sitemaps_rotate_changed_first_then_oldest_fetched() -> None:
    index = parse_sitemap((PAGES / "sitemap-index.xml").read_bytes(), "https://sportano.hu/")
    first, second = (entry.url for entry in index[1:])
    fresh = choose_child_sitemaps(index, "product", {}, limit=1)
    assert [entry.url for entry in fresh] == [first]  # never fetched; categories filtered out
    fetched = {first: NOW, second: NOW - timedelta(days=30)}
    assert [entry.url for entry in choose_child_sitemaps(index, "product", fetched, 1)] == [second]
    fetched = {first: datetime(2026, 10, 22, tzinfo=UTC), second: NOW}
    # products_hu_1 changed (lastmod 23 Oct) after we fetched it (22 Oct).
    assert [entry.url for entry in choose_child_sitemaps(index, "product", fetched, 1)] == [first]


def test_refresh_priorities_put_changed_and_stale_offers_first() -> None:
    known = {
        "https://shop.hu/changed": {"last_checked": (NOW - timedelta(hours=2)).isoformat()},
        "https://shop.hu/fresh": {"last_checked": (NOW - timedelta(hours=2)).isoformat()},
        "https://shop.hu/stale": {
            "last_checked": (NOW - timedelta(days=5)).isoformat(),
            "availability": "in_stock",
        },
        "https://shop.hu/gone": {"state": "gone", "gone_hits": 2},
    }
    ordered = prioritise(
        [
            ("https://shop.hu/new", NOW - timedelta(days=1)),
            ("https://shop.hu/fresh", NOW - timedelta(days=1)),
            ("https://shop.hu/changed", NOW - timedelta(hours=1)),
            ("https://shop.hu/stale", None),
            ("https://shop.hu/gone", None),
        ],
        known,
        now=NOW,
    )
    assert [item.url.rsplit("/", 1)[1] for item in ordered] == ["changed", "stale", "new", "gone"]
    assert ordered[0].priority == PRIORITY_CHANGED_UPSTREAM
    assert ordered[2].priority == PRIORITY_NEW


def test_pagination_rules() -> None:
    query = {"type": "query", "param": "page", "max_pages": 3}
    url = "https://shop.hu/c/kabatok"
    assert next_listing_page(url, query, rel_next=None, found_products=True) == url + "?page=2"
    assert next_listing_page(url + "?page=3", query, rel_next=None, found_products=True) is None
    assert next_listing_page(url, query, rel_next=None, found_products=False) is None
    rel = {"type": "rel-next"}
    assert (
        next_listing_page(url, rel, rel_next="/c/kabatok?page=2", found_products=True)
        == "https://shop.hu/c/kabatok?page=2"
    )


def _xml(url: str, name: str, meta: dict) -> Response:
    return XmlResponse(url, body=(PAGES / name).read_bytes(), request=Request(url, meta=meta))


def test_sitemap_discovery_requests_only_new_products_newest_first() -> None:
    source = SOURCES["sportano"]
    state = CrawlState()
    spider = ProductSpider(
        [source],
        state,
        10,
        mode="sitemap",
        known={"sportano": {"https://sportano.hu/futokabat-sp1"}},
    )
    [index_request] = list(spider.start_requests())
    children = list(
        spider.parse_sitemap(
            _xml(index_request.url, "sitemap-index.xml", index_request.meta), source
        )
    )
    assert [request.url.rsplit("/", 1)[1] for request in children] == [
        "products_hu_1.xml.gz",
        "products_hu_2.xml.gz",
        "categories_hu.xml",
    ][: len(children)]
    first = children[0]
    product_requests = list(
        spider.parse_sitemap(_xml(first.url, "products_hu_1.xml.gz", first.meta), source)
    )
    product_requests += list(spider.failed(_Failure(children[1])))  # the second one failed
    product_requests += [
        request for child in children[2:] for request in spider.failed(_Failure(child))
    ]
    urls = [request.url for request in product_requests]
    assert urls[0] == "https://sportano.hu/kotott-pulover-sp77"  # newest lastmod first
    assert "https://sportano.hu/futokabat-sp1" not in urls  # already in the catalogue
    assert not any("other-shop" in url for url in urls)
    assert state.skipped["sportano"]["already in the catalogue"] == 1


class _Failure:
    def __init__(self, request: Request):
        self.request = request
        self.value = ConnectionError("unreachable")

"""Polite, robots-aware HTTP collection for Hungarian storefronts.

Discovery comes from category listings (with pagination), sitemaps or a refresh
plan; extraction from the LPP embedded listing, H&M category cards, or the generic
schema.org extractor in ``structured_data``. Politeness and access control live in
``politeness``; resumable state in ``crawl_state``.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin, urlsplit

import scrapy
from scrapy.crawler import Crawler, CrawlerProcess
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Response
from scrapy.utils.reactor import is_reactor_installed
from scrapy_playwright.page import PageMethod

from .category_mapping import category_from_slug_extended
from .crawl_state import CrawlStore, instant, product_key
from .discovery import (
    choose_child_sitemaps,
    next_listing_page,
    parse_sitemap,
    prioritise,
    sitemaps_from_robots,
    with_page,
    without_page,
)
from .image_selection import rank_images
from .politeness import (
    DEFAULT_DELAY_SECONDS,
    MIN_DELAY_SECONDS,
    Telemetry,
    polite_settings,
)
from .schema import RawProductRecord, parse_money, utc_now
from .sources import (
    CRAWL_SOURCES,
    SOURCES,  # noqa: F401 - compatibility for existing callers
    USER_AGENT,  # noqa: F401 - compatibility for existing callers
    Source,
    allowed_url,
    user_agent,
)
from .structured_data import MISSING, extract

MAX_LIMIT = 60_000
MAX_REQUESTS = 5_000
MODES = ("auto", "listing", "sitemap", "refresh")
ASYNCIO_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
NOTHING_PLANNED = "plan has nothing to fetch"
LISTING_TIMEOUT_SECONDS = 90  # cumulative LPP pages can be tens of megabytes

LPP_CATALOGUE = "window.getCatalogData"
LPP_PRODUCTS = re.compile(r"\n\s*products\s*:\s*(?=\[)")
LPP_PAGING_KEYS = ("page", "maxPage", "pageSize", "startSize", "currentSize", "productsQuantity")


def listing_category(source: Source, url: str) -> str | None:
    """Category key of a listing page from configured paths or its URL slug."""
    if re.search(source.product_pattern or r"(?!)", url, re.IGNORECASE):
        return None
    path = urlsplit(url).path.rstrip("/")
    for prefix, category in source.category_paths.items():
        if path == prefix or path.startswith(prefix + "/"):
            return category or None
    return category_from_slug_extended(path.rsplit("/", 1)[-1], source.slug_categories)


def source_for_page(source: Source, url: str) -> Source:
    if source.listing != "lpp":
        return source
    return replace(source, category=listing_category(source, url))


def product_links(
    response: Response, source: Source, limit: int, excluded: set[str] | None = None
) -> list[str]:
    if source.product_pattern is None:
        raise ValueError("feed-only sources have no page parser")
    if source.card_selector:
        candidates = response.css(source.card_selector).getall()
    else:
        candidates = response.css("a::attr(href)").getall()
    links: list[str] = []
    seen: set[str] = set()
    for href in candidates:
        url = urljoin(response.url, href)
        if not allowed_url(source, url) or not re.search(
            source.product_pattern, url, re.IGNORECASE
        ):
            continue
        if url not in seen and (excluded is None or url not in excluded):
            seen.add(url)
            links.append(url)
        if len(links) >= limit:
            break
    return links


def extract_hm_cards(
    response: Response, source: Source, limit: int, excluded: set[str] | None = None
) -> list[RawProductRecord]:
    """Read complete H&M offers from public category cards without product requests."""
    records: list[RawProductRecord] = []
    seen: set[str] = set()
    for card in response.css("article[data-articlecode]"):
        url = urljoin(response.url, card.css('a[href*="productpage."]::attr(href)').get() or "")
        sku = card.attrib.get("data-articlecode")
        name = card.css('a[href*="productpage."]::attr(title)').get()
        prices = card.xpath('.//span[contains(normalize-space(text()), "Ft")]/text()').getall()
        prices = [price.strip() for price in prices if price.strip()]
        if (
            not allowed_url(source, url)
            or not re.search(source.product_pattern, url, re.IGNORECASE)
            or not sku
            or not name
            or len(prices) != 1
            or url in seen
            or (excluded is not None and url in excluded)
        ):
            continue
        image = card.css("img::attr(data-src)").get() or card.css("img::attr(src)").get()
        seen.add(url)
        records.append(
            RawProductRecord(
                source_key="hm",
                market_code="HU",
                source_url=response.url,
                fetched_at=utc_now(),
                adapter_version="hm-category-v1",
                product={
                    "url": url,
                    "sku": sku,
                    "name": name,
                    "category": source.category,
                    "price": prices[0],
                    "currency": "HUF",
                    "images": [image] if image and image.startswith("https://") else [],
                },
            )
        )
        if len(records) >= limit:
            break
    return records


def lpp_catalogue(response: Response) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Read the product list and paging numbers an LPP category page embeds."""
    text = response.text
    start = text.find(LPP_CATALOGUE)
    match = LPP_PRODUCTS.search(text, start) if start >= 0 else None
    if match is None:
        raise ValueError("no embedded storefront catalogue")
    try:
        products, _ = json.JSONDecoder(parse_float=Decimal).raw_decode(text, match.end())
    except json.JSONDecodeError as exc:
        raise ValueError("malformed embedded storefront catalogue") from exc
    products = products if isinstance(products, list) else []
    paging = {}
    for key in LPP_PAGING_KEYS:
        found = re.compile(rf"\n\s*{key}\s*:\s*(\d+)\s*,").search(text, start)
        if found:
            paging[key] = int(found[1])
    return [item for item in products if isinstance(item, dict)], paging


def lpp_record(item: dict[str, Any], source: Source, page_url: str) -> RawProductRecord:
    url = urljoin(page_url, str(item.get("url") or ""))
    if not allowed_url(source, url) or not re.search(
        source.product_pattern or "", url, re.IGNORECASE
    ):
        raise ValueError("product URL is outside the Hungarian storefront")
    currency = item.get("currency")

    def first(*keys: str) -> Any:
        # An explicit null must fall back to the next field, not end the lookup.
        return next((item[key] for key in keys if item.get(key) is not None), None)

    price = first("minQtyFinalPrice", "final_price")
    regular = first("minQtyRegularPrice", "price")
    try:
        discounted = parse_money(regular, currency) > parse_money(price, currency)
    except (TypeError, ValueError):
        discounted = False
    images = []
    for photo in item.get("gallery") or []:
        if not isinstance(photo, dict) or photo.get("type") != "photo":
            continue
        original = (photo.get("sizes") or {}).get("original")
        if isinstance(original, str) and original.startswith("https://"):
            images.append(original)
    sizes = [size for size in item.get("sizes") or [] if isinstance(size, dict)]
    stock = [size.get("stock") for size in sizes]
    availability = (
        "in_stock"
        if any(flag is True for flag in stock)  # `True in [1]` is also true
        else "out_of_stock"
        if stock and all(flag is False for flag in stock)
        else "unknown"
    )
    options = [option for option in item.get("colorOptions") or [] if isinstance(option, dict)]
    colours = [
        option["color"]["name"]
        for option in options
        if option.get("sku") == item.get("sku")
        and isinstance(option.get("color"), dict)
        and option["color"].get("name")
    ]
    # Every colour of one model lists the same colour options, so the smallest SKU
    # among them is a stable group key without assuming how LPP builds SKUs.
    group_skus = sorted({str(option["sku"]) for option in options if option.get("sku")})
    merchant = item.get("merchant")
    return RawProductRecord(
        source_key=source.key,
        market_code="HU",
        source_url=page_url,
        fetched_at=utc_now(),
        adapter_version="lpp-catalogue-v2",
        product={
            "url": url,
            "sku": item.get("sku"),
            "item_group_id": f"colours:{group_skus[0]}" if len(group_skus) > 1 else None,
            "name": item.get("name"),
            "brand": merchant.get("name") if isinstance(merchant, dict) else None,
            "category": source.category,
            "price": price,
            "currency": currency,
            "original_price": regular if discounted else None,
            "images": rank_images(images),
            "colours": colours,
            "sizes": [size["sizeName"] for size in sizes if size.get("sizeName")],
            "availability": availability,
            "_source_payload": {
                key: item[key]
                for key in ("id", "sku", "name", "url", "currency", "final_price", "price", "sizes")
                if key in item
            },
        },
    )


def unique_by_sku(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Storefronts list one product under alias URLs (``coat-2-…``); keep the shortest URL."""
    chosen: dict[Any, dict[str, Any]] = {}
    for item in items:
        key = item.get("sku") or item.get("url")
        current = chosen.get(key)
        if current is None or len(str(item.get("url"))) < len(str(current.get("url"))):
            chosen[key] = item  # an existing key keeps its first listing position
    return list(chosen.values())


def lpp_next_page(paging: dict[str, int], wanted: int) -> int | None:
    """Pick the listing page that holds ``wanted`` products, or None when done.

    LPP listings are cumulative: page N repeats pages 1..N-1 and appends
    ``pageSize`` more products, so a single request can reach deep into a category.
    """
    page, last = paging.get("page", 1), paging.get("maxPage", 1)
    shown, total = paging.get("currentSize"), paging.get("productsQuantity")
    if page >= last or (shown is not None and total is not None and shown >= total):
        return None
    start, size = paging.get("startSize", paging.get("pageSize")), paging.get("pageSize")
    if not start or not size:
        return page + 1
    needed = 1 + -(-max(0, wanted - start) // size)
    return min(last, max(page + 1, needed))


def _record(
    source: Source, page_url: str, product: dict[str, Any], method: str
) -> RawProductRecord:
    return RawProductRecord(
        source_key=source.key,
        market_code="HU",
        source_url=page_url,
        fetched_at=utc_now(),
        adapter_version=f"structured-{method}-v1",
        product=product,
    )


def extract_records(
    response: Response,
    source: Source,
    *,
    listing: bool = False,
    category: str | None = None,
) -> tuple[list[RawProductRecord], list[str], set[str]]:
    """Records, rejection reasons and robots directives from one page."""
    if not allowed_url(source, response.url):
        raise ValueError("fetched page is outside the Hungarian storefront")
    header = response.headers.get(b"X-Robots-Tag")
    result = extract(
        response.text,
        response.url,
        source,
        listing_category=category if category is not None else source.category,
        listing=listing,
        robots_header=header.decode("latin-1") if header else None,
    )
    records = [
        _record(source, response.url, product, result.method or "unknown")
        for product in result.products
    ]
    return records, result.rejected, result.robots


def extract_product(response: Response, source: Source) -> RawProductRecord:
    """The page's own product (the one at the page URL, else the first one found)."""
    records, rejected, _ = extract_records(response, source)
    if not records:
        raise ValueError(rejected[0] if rejected else MISSING)
    page = product_key(response.url)
    return next(
        (record for record in records if product_key(str(record.product["url"])) == page),
        records[0],
    )


@dataclass
class CrawlState:
    records: dict[str, list[RawProductRecord]] = field(default_factory=dict)
    errors: dict[str, list[str]] = field(default_factory=dict)
    discovered: dict[str, int] = field(default_factory=dict)
    telemetry: Telemetry | None = None
    # Listing root URL -> product URLs it showed, and roots read to their last page.
    listing_seen: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    complete_listings: dict[str, set[str]] = field(default_factory=dict)
    gone: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    skipped: dict[str, Counter] = field(default_factory=dict)
    # Planned URL -> what happened to it (refresh and benchmark runs).
    outcomes: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    # One entry per fetched page, for the extraction benchmark.
    pages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    listing_totals: dict[str, int] = field(default_factory=dict)

    def error(self, source: str, message: str) -> None:
        self.errors.setdefault(source, []).append(message)

    def skip(self, source: str, reason: str, count: int = 1) -> None:
        self.skipped.setdefault(source, Counter())[reason] += count

    def outcome(self, source: str, url: str, outcome: str, **details: Any) -> None:
        self.outcomes.setdefault(source, {})[url] = {"outcome": outcome, **details}


@dataclass(frozen=True)
class PlanEntry:
    url: str
    kind: str  # listing | product | sitemap | robots-sitemaps
    priority: int = 0
    reason: str = ""


JS_MARKERS = re.compile(
    rb'id="__next"|id="root"\s*>\s*</div>|ng-version=|data-reactroot|"serverRenderingMode"\s*:\s*"csr"'
    rb"|<noscript>[^<]{0,200}(?:JavaScript|javascript)",
)


def js_markers(response: Response) -> bool:
    """A client-rendered shell: SPA markers and little server-rendered text."""
    body = response.body[:500_000] if response.body else b""
    if not JS_MARKERS.search(body):
        return False
    text = re.sub(rb"<script.*?</script>|<style.*?</style>|<[^>]+>", b" ", body, flags=re.DOTALL)
    return len(re.sub(rb"\s+", b" ", text).strip()) < 3000


class ProductSpider(scrapy.Spider):
    name = "fashion_finder_products"

    def __init__(
        self,
        sources: list[Source],
        state: CrawlState,
        limit: int,
        overrides: dict[str, list[str]] | None = None,
        browser: bool = False,
        browser_wait_ms: int = 1500,
        *,
        mode: str = "auto",
        plans: dict[str, list[PlanEntry]] | None = None,
        store: CrawlStore | None = None,
        run_ids: dict[str, str] | None = None,
        telemetry: Telemetry | None = None,
        all_categories: bool = False,
        max_sitemaps: int = 3,
        known: dict[str, set[str]] | None = None,
        benchmark_pages: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.sources = sources
        self.state = state
        self.limit = limit
        self.overrides = overrides or {}
        self.browser = browser
        self.browser_wait_ms = browser_wait_ms
        self.mode = mode
        self.plans = plans or {}
        self.store = store
        self.run_ids = run_ids or {}
        self.telemetry = telemetry or Telemetry(sources, store=store)
        state.telemetry = self.telemetry
        self.all_categories = all_categories
        self.max_sitemaps = max_sitemaps
        self.known = known or {}
        self.benchmark_pages = benchmark_pages
        self.benchmarked: Counter = Counter()
        self.allowed_domains = [source.host for source in sources]
        self.scheduled_products: dict[str, set[str]] = {source.key: set() for source in sources}
        self.listed_skus: dict[str, set[str]] = {}
        self.pending_sitemaps: Counter = Counter()
        self.sitemap_candidates: dict[str, dict[str, datetime | None]] = {}
        self._restore()

    @property
    def refreshing(self) -> bool:
        return self.mode == "refresh"

    # ---------------------------------------------------------------- bookkeeping
    def _restore(self) -> None:
        """On a resumed run, count products already spooled toward the limit."""
        if self.store is None:
            return
        for source in self.sources:
            run_id = self.run_ids.get(source.key)
            if not run_id:
                continue
            for entry in self.store.frontier(run_id):
                if entry["kind"] == "product" and entry["status"] != "pending":
                    self.scheduled_products[source.key].add(entry["url"])
            for record in self.store.spooled(run_id):
                self.scheduled_products[source.key].add(str(record.product.get("url")))

    def _is_known(self, source_key: str, url: str) -> bool:
        return product_key(url) in self.known.get(source_key, ())

    def _remaining(self, source_key: str) -> int:
        return self.limit - len(self.scheduled_products[source_key])

    def _reserve_product(self, source_key: str, url: str) -> bool:
        scheduled = self.scheduled_products[source_key]
        if url in scheduled or len(scheduled) >= self.limit:
            return False
        scheduled.add(url)
        self.state.discovered[source_key] = self.state.discovered.get(source_key, 0) + 1
        return True

    def _add_records(self, source_key: str, records: list[RawProductRecord]) -> None:
        if not records:
            return
        self.state.records.setdefault(source_key, []).extend(records)
        run_id = self.run_ids.get(source_key)
        if self.store is not None and run_id:
            self.store.spool(run_id, records)

    def _done(self, source_key: str, response: Response, status: str = "done") -> None:
        run_id = self.run_ids.get(source_key)
        if self.store is not None and run_id and response.request is not None:
            self.store.mark(run_id, response.request.url, status, str(response.status))

    def _requested(self, response: Response) -> str:
        return response.request.url if response.request is not None else response.url

    def _kind(self, response: Response, source: Source) -> str:
        meta_kind = response.request.meta.get("kind") if response.request is not None else None
        if meta_kind:
            return meta_kind
        urls = [self._requested(response), response.url]
        if source.product_pattern is None or any(
            re.search(source.product_pattern, url, re.IGNORECASE) for url in urls
        ):
            return "product"
        return "listing"

    def _log_page(
        self,
        response: Response,
        source: Source,
        kind: str,
        *,
        method: str | None,
        records: list[RawProductRecord],
        error: str | None = None,
    ) -> None:
        products = [record.product for record in records]
        self.state.pages.setdefault(source.key, []).append(
            {
                "url": self._requested(response),
                "kind": kind,
                "status": response.status,
                "latency": (
                    response.request.meta.get("download_latency") if response.request else None
                ),
                "method": method,
                "records": len(records),
                "price": any(product.get("price") is not None for product in products),
                "sizes": any(product.get("sizes") for product in products),
                "js_markers": js_markers(response) if not records else False,
                "browser": bool(response.request and response.request.meta.get("playwright")),
                "error": error,
            }
        )

    def _request_meta(self, source_key: str) -> dict[str, Any]:
        meta: dict[str, Any] = {"source_key": source_key}
        if self.browser:
            meta["playwright"] = True
            meta["playwright_page_goto_kwargs"] = {"wait_until": "domcontentloaded"}
            if self.browser_wait_ms:
                meta["playwright_page_methods"] = [
                    PageMethod("wait_for_timeout", self.browser_wait_ms)
                ]
        return meta

    def _request(
        self, source: Source, url: str, kind: str, priority: int = 0, reason: str = ""
    ) -> scrapy.Request:
        meta = self._request_meta(source.key) | {"kind": kind}
        if not self.browser and (kind == "sitemap" or (kind == "product" and self.refreshing)):
            meta["conditional"] = True
        if kind == "listing":
            meta["download_timeout"] = LISTING_TIMEOUT_SECONDS
        callback = {
            "sitemap": self.parse_sitemap,
            "robots-sitemaps": self.parse_robots_sitemaps,
        }.get(kind, self.parse_page if kind == "listing" else self.parse_product)
        run_id = self.run_ids.get(source.key)
        if self.store is not None and run_id:
            self.store.enqueue(run_id, source.key, url, kind, priority, {"reason": reason})
        if kind == "sitemap":
            self.pending_sitemaps[source.key] += 1
        return scrapy.Request(
            url,
            callback=callback,
            errback=self.failed,
            cb_kwargs={"source": source},
            meta=meta,
            priority=priority,
            dont_filter=kind == "robots-sitemaps",
        )

    # ---------------------------------------------------------------- start
    def _default_entries(self, source: Source) -> list[PlanEntry]:
        urls = self.overrides.get(source.key)
        mode = self.mode
        if urls is None and mode in {"auto", "listing"}:
            urls = [source.category_url] if source.category_url else []
            if self.all_categories:
                urls += [url for url in source.category_urls if url not in urls]
        if urls:
            entries = []
            for url in urls:
                is_product = source.product_pattern is None or re.search(
                    source.product_pattern, url, re.IGNORECASE
                )
                entries.append(PlanEntry(url, "product" if is_product else "listing"))
            return entries
        if mode in {"auto", "sitemap"} and source.sitemaps:
            return [PlanEntry(url, "sitemap") for url in source.sitemaps]
        if mode == "sitemap":
            return [PlanEntry(f"https://www.{source.host}/robots.txt", "robots-sitemaps")]
        return []

    def start_requests(self):
        for source in self.sources:
            if not source.crawl_enabled:
                raise ValueError("feed-only sources cannot be crawled")
            entries = self.plans.get(source.key)
            if entries is None:
                entries = self._default_entries(source)
                if not entries:
                    self.state.error(
                        source.key,
                        "no start URL configured for this mode; pass --url or a sitemap",
                    )
                    continue
            elif not entries:
                self.state.skip(source.key, NOTHING_PLANNED)
                continue
            for entry in entries:
                if entry.kind in {"listing", "product"} and not allowed_url(source, entry.url):
                    self.state.error(source.key, "input URL is outside the Hungarian storefront")
                    self.state.outcome(source.key, entry.url, "failed", reason="URL not allowed")
                    continue
                if entry.kind == "listing" and self._remaining(source.key) <= 0:
                    break
                if entry.kind == "product" and not self._reserve_product(source.key, entry.url):
                    continue
                yield self._request(source, entry.url, entry.kind, entry.priority, entry.reason)

    async def start(self):
        # Scrapy 2.13+ calls start(); older supported versions use start_requests().
        for request in self.start_requests():
            yield request

    def closed(self, reason: str) -> None:
        for source in self.sources:
            stats = self.telemetry.get(source.key)
            if stats is not None:
                stats.finished = time.monotonic()
        if reason not in {"finished", None}:
            for source in self.sources:
                self.state.skip(source.key, f"crawl closed early: {reason}")

    # ---------------------------------------------------------------- errors
    def failed(self, failure: Any) -> list[scrapy.Request]:
        request = failure.request
        source_key = request.meta["source_key"]
        response = getattr(failure.value, "response", None)
        status = f" HTTP {response.status}" if response is not None else ""
        skipped = False
        if isinstance(failure.value, IgnoreRequest):
            message = f"request skipped: {failure.value}"
            skipped = "robots" not in str(failure.value).lower()
        else:
            lines = str(failure.value).strip().splitlines()
            detail = lines[0][:300] if lines else ""
            fatal = next((line.strip() for line in lines if "FATAL:" in line), "")
            if fatal and fatal != detail:
                detail += f" | {fatal[:200]}"
            suffix = f": {detail}" if detail else ""
            message = f"request failed: {type(failure.value).__name__}{status}{suffix}"
        self.state.error(source_key, message)
        reason = (
            "robots.txt disallows this URL"
            if "robots" in message.lower()
            else f"HTTP {response.status}"
            if response is not None
            else type(failure.value).__name__
        )
        self.state.outcome(
            source_key,
            request.url,
            "skipped" if skipped else "failed",
            reason=reason,
            status=response.status if response is not None else None,
            kind=request.meta.get("kind"),
        )
        run_id = self.run_ids.get(source_key)
        if self.store is not None and run_id:
            self.store.mark(run_id, request.url, "failed", message[:200])
        source = next((item for item in self.sources if item.key == source_key), None)
        if request.meta.get("kind") == "sitemap" and source is not None:
            return list(self._sitemap_finished(source))
        return []

    # ---------------------------------------------------------------- sitemaps
    def parse_robots_sitemaps(self, response: Response, source: Source):
        urls = [
            url
            for url in sitemaps_from_robots(response.text)
            if (urlsplit(url).hostname or "").endswith(source.host)
        ]
        if not urls:
            self.state.error(source.key, "robots.txt lists no sitemap on the shop's domain")
        for url in urls:
            yield self._request(source, url, "sitemap")
        self._done(source.key, response)

    def parse_sitemap(self, response: Response, source: Source):
        self._done(source.key, response)
        if response.status in (404, 410):
            self.state.error(source.key, f"sitemap HTTP {response.status}: {response.url}")
            yield from self._sitemap_finished(source)
            return
        if response.status == 304:
            self.state.skip(source.key, "sitemap not modified")
            yield from self._sitemap_finished(source)
            return
        try:
            entries = parse_sitemap(response.body, response.url)
        except ValueError as exc:
            self.state.error(source.key, f"sitemap unreadable: {exc}")
            yield from self._sitemap_finished(source)
            return
        if entries and entries[0].is_sitemap:
            fetched = {}
            if self.store is not None:
                for entry in entries:
                    validators = self.store.validators(entry.url)
                    fetched[entry.url] = instant(validators["fetched_at"]) if validators else None
            chosen = choose_child_sitemaps(
                entries, source.sitemap_follow, fetched, self.max_sitemaps
            )
            if not chosen:
                names = ", ".join(entry.url.rsplit("/", 1)[-1] for entry in entries[:5])
                self.state.error(
                    source.key, f"no child sitemap matches sitemap_follow; saw {names}"
                )
            for entry in chosen:
                yield self._request(source, entry.url, "sitemap")
        else:
            candidates = self.sitemap_candidates.setdefault(source.key, {})
            for entry in entries:
                if not allowed_url(source, entry.url):
                    continue
                if source.product_pattern and not re.search(
                    source.product_pattern, entry.url, re.IGNORECASE
                ):
                    continue
                candidates[entry.url] = entry.lastmod
            self.state.listing_totals[source.key] = self.state.listing_totals.get(
                source.key, 0
            ) + len(entries)
        yield from self._sitemap_finished(source)

    def _sitemap_finished(self, source: Source):
        self.pending_sitemaps[source.key] -= 1
        if self.pending_sitemaps[source.key] > 0:
            return
        candidates = self.sitemap_candidates.pop(source.key, {})
        if not candidates:
            return
        fresh = {
            url: lastmod
            for url, lastmod in candidates.items()
            if not self._is_known(source.key, url)
        }
        self.state.skip(source.key, "already in the catalogue", len(candidates) - len(fresh))
        ordered = prioritise(fresh.items(), {})  # discovery: newest lastmod first
        if self.benchmark_pages:
            ordered = _spread(ordered, self.benchmark_pages)
        for candidate in ordered:
            if not self._reserve_product(source.key, candidate.url):
                continue
            yield self._request(
                source, candidate.url, "product", candidate.priority, candidate.reason
            )

    # ---------------------------------------------------------------- pages
    def parse_product(self, response: Response, source: Source):
        yield from self.parse_page(response, source)

    def parse_page(self, response: Response, source: Source):
        kind = self._kind(response, source)
        is_product_url = kind == "product"
        requested = self._requested(response)
        if response.status == 304:
            self.state.outcome(source.key, requested, "not_modified", status=304, kind=kind)
            self._log_page(response, source, kind, method="not modified", records=[])
            self._done(source.key, response)
            return
        if response.status in (404, 410):
            self._done(source.key, response, "gone")
            self.state.outcome(
                source.key,
                requested,
                "gone",
                reason=f"HTTP {response.status}",
                status=response.status,
                kind=kind,
            )
            self._log_page(
                response, source, kind, method=None, records=[], error=f"HTTP {response.status}"
            )
            if is_product_url:
                self.state.gone.setdefault(source.key, []).append((response.url, response.status))
            else:
                self.state.error(source.key, f"listing page HTTP {response.status}")
            return
        page_source = source_for_page(source, response.url)
        if not is_product_url and self._remaining(source.key) <= 0:
            self._done(source.key, response, "skipped")
            return
        if source.listing == "hm-cards" and not is_product_url:
            records = extract_hm_cards(
                response,
                page_source,
                self._remaining(source.key),
                self.scheduled_products[source.key],
            )
            added = [
                record
                for record in records
                if self._reserve_product(source.key, str(record.product["url"]))
            ]
            self._add_records(source.key, added)
            self._log_page(response, source, kind, method="html-selectors", records=added)
            if not added and self._remaining(source.key) > 0:
                self.state.error(source.key, "no complete HUF product cards on category page")
            self._done(source.key, response)
            return
        if source.listing == "lpp" and not is_product_url:
            try:
                items, paging = lpp_catalogue(response)
            except ValueError as exc:
                if self.refreshing:  # a refresh needs the full listing; report, do not guess
                    self.state.error(source.key, f"listing refresh failed: {exc}")
                    self.state.outcome(
                        source.key, requested, "failed", reason=str(exc), kind="listing"
                    )
                    self._done(source.key, response)
                    return
                # discovery: older markup falls back to product links below
            else:
                yield from self.parse_lpp_listing(response, page_source, items, paging)
                self._done(source.key, response)
                return
        if is_product_url:
            self._product_page(response, page_source)
            self._done(source.key, response)
            return
        yield from self._listing_page(response, page_source)
        self._done(source.key, response)

    def _product_page(self, response: Response, source: Source) -> None:
        requested = self._requested(response)
        try:
            records, rejected, robots = extract_records(response, source)
        except (ValueError, TypeError) as exc:
            records, rejected, robots = [], [str(exc)], set()
        method = records[0].adapter_version.split("-")[1] if records else None
        if "noai" in robots:
            self.telemetry.stop(source.key, "page opts out of AI use (robots noai)")
            self.state.outcome(source.key, requested, "skipped", reason="robots noai")
            return
        if "noindex" in robots:
            self.state.skip(source.key, "product page asks not to be indexed (noindex)")
            self.state.outcome(source.key, requested, "failed", reason="robots noindex")
            return
        if "noimageai" in robots:
            for record in records:
                record.product["images"] = []
            self.state.skip(source.key, "photos withheld (noimageai)", len(records))
        self._log_page(
            response,
            source,
            "product",
            method=method,
            records=records,
            error=None if records else (rejected[0] if rejected else MISSING),
        )
        if not records:
            redirected = product_key(response.url) != product_key(requested)
            reason = rejected[0] if rejected else MISSING
            self.state.error(source.key, f"product extraction failed: {reason}")
            self.state.outcome(
                source.key,
                requested,
                "gone" if redirected else "failed",
                reason="redirected to a non-product page"
                if redirected
                else f"extraction: {reason}",
                status=response.status,
                kind="product",
            )
            return
        for reason in rejected:
            self.state.skip(source.key, f"variant group skipped: {reason}")
        if any(record.product.get("discontinued") for record in records):
            self.state.outcome(source.key, requested, "gone", reason="discontinued", kind="product")
            return
        self.state.outcome(
            source.key,
            requested,
            "ok",
            kind="product",
            urls=[product_key(str(record.product["url"])) for record in records],
            skus=[record.product.get("sku") for record in records],
        )
        self._add_records(source.key, records)

    def _listing_page(self, response: Response, source: Source):
        root = without_page(response.url, source.pagination.get("param", "page"))
        seen = self.state.listing_seen.setdefault(source.key, {}).setdefault(root, set())
        stored: list[RawProductRecord] = []
        if source.extraction.get("listing_structured_data"):
            try:
                records, _, _ = extract_records(response, source, listing=True)
            except (ValueError, TypeError):
                records = []
            for record in records:
                url = str(record.product["url"])
                seen.add(product_key(url))
                if self._reserve_product(source.key, url):
                    stored.append(record)
            self._add_records(source.key, stored)
        links = []
        if source.product_pattern is not None:
            links = product_links(
                response,
                source,
                max(self._remaining(source.key), 0) + len(self.scheduled_products[source.key]),
            )
            seen.update(product_key(url) for url in links)
            links = [
                url
                for url in links
                if url not in self.scheduled_products[source.key]
                and (self.benchmark_pages or not self._is_known(source.key, url))
            ]
        self._log_page(
            response, source, "listing", method="listing links" if links else None, records=stored
        )
        if not links and not stored:
            try:
                record = extract_product(response, source)
            except (ValueError, TypeError) as exc:
                self.state.error(
                    source.key, f"no product records or links found on starting page: {exc}"
                )
                return
            if record.product.get("price") is not None and self._reserve_product(
                source.key, str(record.product["url"])
            ):
                self._add_records(source.key, [record])
            return
        self.state.outcome(source.key, self._requested(response), "ok", kind="listing")
        if self.benchmark_pages:
            links = _spread(links, self.benchmark_pages - self.benchmarked[source.key])
            self.benchmarked[source.key] += len(links)
        for url in links:
            if not self._reserve_product(source.key, url):
                continue
            yield self._request(source, url, "product")
        if self.benchmark_pages:
            return
        rel_next = response.css('link[rel="next"]::attr(href), a[rel="next"]::attr(href)').get()
        following = next_listing_page(
            response.url, source.pagination, rel_next=rel_next, found_products=bool(links or stored)
        )
        if following is None:
            if self._remaining(source.key) > 0:
                self.state.complete_listings.setdefault(source.key, set()).add(root)
        elif self._remaining(source.key) > 0:
            yield self._request(source, following, "listing")

    def parse_lpp_listing(
        self,
        response: Response,
        source: Source,
        items: list[dict[str, Any]],
        paging: dict[str, int],
    ):
        """Store listed offers directly; request a deeper page only if the limit needs it.

        A refresh reads the whole category (one jump to the page that holds every
        product), so offers missing from it can be checked on their own pages.
        """
        root = without_page(response.url)
        seen = self.state.listing_seen.setdefault(source.key, {}).setdefault(root, set())
        if "productsQuantity" in paging:
            self.state.listing_totals[source.key] = max(
                self.state.listing_totals.get(source.key, 0), paging["productsQuantity"]
            )
        added: list[RawProductRecord] = []
        seen_skus = self.listed_skus.setdefault(source.key, set())
        truncated = False
        for item in unique_by_sku(items):
            url = urljoin(response.url, str(item.get("url") or ""))
            seen.add(product_key(url))
            if self._remaining(source.key) <= 0:
                truncated = True
                continue
            if item.get("sku") in seen_skus:
                continue
            try:
                record = lpp_record(item, source, response.url)
            except (ValueError, TypeError) as exc:
                self.state.error(source.key, f"catalogue product skipped: {exc}")
                continue
            if self._reserve_product(source.key, str(record.product["url"])):
                added.append(record)
                if record.product["sku"]:
                    seen_skus.add(record.product["sku"])
        self._add_records(source.key, added)
        self._log_page(response, source, "listing", method="embedded-json", records=added)
        self.state.outcome(source.key, self._requested(response), "ok", kind="listing")
        if not items:
            self.state.error(source.key, "embedded storefront catalogue lists no products")
            return
        if self.benchmark_pages:
            sample = _spread(
                [str(record.product["url"]) for record in added],
                self.benchmark_pages - self.benchmarked[source.key],
            )
            self.benchmarked[source.key] += len(sample)
            for url in sample:
                yield self._request(source, url, "product")
            return
        if truncated:
            return
        wanted = (
            paging.get("productsQuantity", 0)
            if self.refreshing
            else paging.get("currentSize", len(items)) + self._remaining(source.key)
        )
        page = lpp_next_page(paging, wanted)
        if page is None:
            if self._remaining(source.key) > 0:
                self.state.complete_listings.setdefault(source.key, set()).add(root)
            return
        if not added and paging.get("page", 1) > 1:
            return
        yield self._request(source, with_page(response.url, page), "listing")


def _spread(values: list, count: int) -> list:
    """``count`` items evenly spread over ``values`` (a representative sample)."""
    if count <= 0:
        return []
    if len(values) <= count:
        return list(values)
    step = len(values) / count
    return [values[int(index * step)] for index in range(count)]


def crawl(
    sources: list[Source],
    limit: int,
    overrides: dict[str, list[str]] | None = None,
    browser: bool = False,
    browser_wait_ms: int = 1500,
    *,
    mode: str = "auto",
    max_requests: int = 200,
    delay: float = DEFAULT_DELAY_SECONDS,
    contact: str | None = None,
    store: CrawlStore | None = None,
    run_ids: dict[str, str] | None = None,
    plans: dict[str, list[PlanEntry]] | None = None,
    all_categories: bool = False,
    max_sitemaps: int = 3,
    known: dict[str, set[str]] | None = None,
    benchmark_pages: int = 0,
    time_budget_minutes: float | None = None,
    replay_dir: str | None = None,
) -> CrawlState:
    """Crawl the sources politely; browser-rendered sources run in their own crawler.

    Sources marked ``rendering = "browser"`` (or all, with ``browser=True``) use a
    separate Playwright crawler with one request at a time; everything else uses
    plain HTTP. Both share the run's telemetry and state.
    """
    if any(source.key not in CRAWL_SOURCES for source in sources):
        raise ValueError("feed-only sources cannot be crawled")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    if not 0 <= browser_wait_ms <= 10000:
        raise ValueError("browser wait must be between 0 and 10000 milliseconds")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if not 1 <= max_requests <= MAX_REQUESTS:
        raise ValueError(f"max requests must be between 1 and {MAX_REQUESTS}")
    floor = 0.0 if replay_dir else MIN_DELAY_SECONDS
    if not floor <= delay <= 120:
        raise ValueError(f"delay must be between {floor:g} and 120 seconds")
    state = CrawlState()
    telemetry = Telemetry(sources, max_requests=max_requests, min_delay=delay, store=store)
    state.telemetry = telemetry
    groups = {
        "http": [source for source in sources if source.rendering == "http" and not browser],
        "browser": [source for source in sources if source.rendering == "browser" or browser],
    }

    def settings_for(rendered: bool, group: list[Source]) -> dict[str, Any]:
        settings = polite_settings(
            user_agent=user_agent(contact),
            min_delay=delay,
            timeout=30 if rendered else 20,
            concurrency=max(source.max_concurrency for source in group),
        )
        if time_budget_minutes:
            settings["CLOSESPIDER_TIMEOUT"] = int(time_budget_minutes * 60)
        if replay_dir:
            settings["FASHION_FINDER_REPLAY_DIR"] = replay_dir
            settings["DOWNLOAD_HANDLERS"] = {
                "https": "fashion_finder_data.replay.ReplayDownloadHandler",
                "http": "fashion_finder_data.replay.ReplayDownloadHandler",
            }
        elif rendered:
            settings.update(
                {
                    "DOWNLOAD_HANDLERS": {
                        "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
                    },
                    "PLAYWRIGHT_BROWSER_TYPE": "chromium",
                    "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": 20000,
                    "PLAYWRIGHT_MAX_PAGES_PER_CONTEXT": 1,
                    "CONCURRENT_REQUESTS": 1,
                    "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
                }
            )
        return settings

    process = CrawlerProcess(settings={"TWISTED_REACTOR": ASYNCIO_REACTOR, "LOG_LEVEL": "INFO"})
    for rendering, group in groups.items():
        if not group:
            continue
        crawler = Crawler(
            ProductSpider,
            settings_for(rendering == "browser", group),
            init_reactor=not is_reactor_installed(),
        )
        process.crawl(
            crawler,
            sources=group,
            state=state,
            limit=limit,
            overrides=overrides,
            browser=rendering == "browser",
            browser_wait_ms=browser_wait_ms,
            mode=mode,
            plans=plans,
            store=store,
            run_ids=run_ids,
            telemetry=telemetry,
            all_categories=all_categories,
            max_sitemaps=max_sitemaps,
            known=known,
            benchmark_pages=benchmark_pages,
        )
    process.start()
    for source in sources:
        if NOTHING_PLANNED in state.skipped.get(source.key, {}):
            continue
        if not state.records.get(source.key) and not state.errors.get(source.key):
            reason = telemetry.stopped(source.key)
            if reason or mode != "refresh":
                state.error(
                    source.key,
                    f"no records: {reason}"
                    if reason
                    else "no records; robots policy or storefront access may block collection",
                )
    return state

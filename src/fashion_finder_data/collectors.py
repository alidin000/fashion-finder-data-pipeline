"""Small, robots-aware HTTP collection for Hungarian storefronts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urljoin, urlsplit

import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.http import Response

from .schema import RawProductRecord, utc_now


@dataclass(frozen=True)
class Source:
    key: str
    host: str
    category_url: str
    category: str | None
    product_pattern: str
    card_selector: str | None = None


# Category pages are public storefront URLs. Parsers must be verified against live HTML.
SOURCES = {
    "zara": Source(
        "zara",
        "zara.com",
        "https://www.zara.com/hu/en/woman-jackets-l1114.html",
        "jackets",
        r"-p\d+\.html(?:$|\?)",
    ),
    "hm": Source(
        "hm",
        "hm.com",
        "https://www2.hm.com/hu_hu/noi/vasarlas-termek-szerint/dzsekik/dzsekik.html",
        "jackets",
        r"productpage\.\d+\.html(?:$|\?)",
    ),
    "reserved": Source(
        "reserved",
        "reserved.com",
        "https://www.reserved.com/hu/hu/noi/ruhazat/kabatok",
        "coats",
        r"/hu/hu/[^/]+-[a-z0-9]{3,}-[a-z0-9]{2,}$",
        "article.es-product a.es-product-photo::attr(href)",
    ),
    "pullandbear": Source(
        "pullandbear",
        "pullandbear.com",
        "https://www.pullandbear.com/hu/noi/ruhazat/bluzok-es-ingek-n6525",
        "shirts",
        r"-p\d+\.html(?:$|\?)",
    ),
}

RESERVED_CATEGORIES = {
    "/hu/hu/noi/ruhazat/kabatok": "coats",
    "/hu/hu/noi/kabatok": "coats",
    "/hu/hu/noi/ruhak": "dresses",
    "/hu/hu/noi/puloverek": "sweaters",
}


def source_for_page(source: Source, url: str) -> Source:
    if source.key != "reserved":
        return source
    path = urlsplit(url).path.rstrip("/")
    for prefix, category in RESERVED_CATEGORIES.items():
        if path == prefix or path.startswith(prefix + "/"):
            return replace(source, category=category)
    return replace(source, category=None)


def allowed_url(source: Source, value: str) -> bool:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or port not in (None, 443)
        or not (host == source.host or host.endswith("." + source.host))
    ):
        return False
    return parts.path.startswith("/hu/") or parts.path.startswith("/hu_hu/")


def product_links(response: Response, source: Source, limit: int) -> list[str]:
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
        if url not in seen:
            seen.add(url)
            links.append(url)
        if len(links) >= limit:
            break
    return links


def extract_hm_cards(response: Response, source: Source, limit: int) -> list[RawProductRecord]:
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


def _product_node(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        types = obj.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if any(str(item).rsplit("/", 1)[-1] == "Product" for item in types):
            return obj
        for key in ("@graph", "mainEntity", "itemListElement"):
            node = _product_node(obj.get(key))
            if node:
                return node
    elif isinstance(obj, list):
        for item in obj:
            node = _product_node(item)
            if node:
                return node
    return None


def extract_product(response: Response, source: Source) -> RawProductRecord:
    if not allowed_url(source, response.url):
        raise ValueError("fetched page is outside the Hungarian storefront")
    node: dict[str, Any] | None = None
    for script in response.css('script[type="application/ld+json"]::text').getall():
        try:
            node = _product_node(json.loads(script))
        except json.JSONDecodeError:
            continue
        if node:
            break

    def meta(name: str) -> str | None:
        return response.css(f'meta[property="{name}"]::attr(content)').get()

    if node is None:
        if not meta("product:price:amount"):
            raise ValueError("no Product structured data or product price metadata")
        node = {}
    offers = node.get("offers") or {}
    offers = offers if isinstance(offers, list) else [offers]
    choices = {
        (str(item.get("price")), str(item.get("priceCurrency")))
        for item in offers
        if isinstance(item, dict) and item.get("price") is not None
    }
    if len(choices) > 1:
        raise ValueError("multiple distinct offer prices on product page")
    offer = next((item for item in offers if isinstance(item, dict)), {})
    brand = node.get("brand")
    brand = brand.get("name") if isinstance(brand, dict) else brand
    images = node.get("image") or meta("og:image") or []
    images = images if isinstance(images, list) else [images]
    url = node.get("url") or offer.get("url") or meta("og:url") or response.url
    if not allowed_url(source, str(url)):
        raise ValueError("product URL is outside the Hungarian storefront")
    data = {
        "url": url,
        "sku": node.get("sku") or meta("product:retailer_part_no"),
        "name": node.get("name") or meta("og:title"),
        "brand": brand,
        "category": source.category,
        "description": node.get("description"),
        "price": offer.get("price") or meta("product:price:amount"),
        "currency": offer.get("priceCurrency") or meta("product:price:currency"),
        "original_price": meta("product:original_price:amount"),
        "images": images,
        "colours": node.get("color"),
        "sizes": node.get("size"),
        "availability": offer.get("availability"),
        "_source_payload": node,
    }
    return RawProductRecord(
        source_key=source.key,
        market_code="HU",
        source_url=response.url,
        fetched_at=utc_now(),
        product=data,
    )


@dataclass
class CrawlState:
    records: dict[str, list[RawProductRecord]] = field(default_factory=dict)
    errors: dict[str, list[str]] = field(default_factory=dict)
    discovered: dict[str, int] = field(default_factory=dict)

    def error(self, source: str, message: str) -> None:
        self.errors.setdefault(source, []).append(message)


class ProductSpider(scrapy.Spider):
    name = "fashion_finder_products"

    def __init__(
        self,
        sources: list[Source],
        state: CrawlState,
        limit: int,
        overrides: dict[str, list[str]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.sources = sources
        self.state = state
        self.limit = limit
        self.overrides = overrides or {}
        self.allowed_domains = [source.host for source in sources]

    def start_requests(self):
        for source in self.sources:
            urls = self.overrides.get(source.key, [source.category_url])
            for url in urls[: self.limit]:
                if not allowed_url(source, url):
                    self.state.error(source.key, "input URL is outside the Hungarian storefront")
                    continue
                yield scrapy.Request(
                    url,
                    callback=self.parse_page,
                    errback=self.failed,
                    cb_kwargs={"source": source},
                    meta={"source_key": source.key},
                )

    async def start(self):
        # Scrapy 2.13+ calls start(); older supported versions use start_requests().
        for request in self.start_requests():
            yield request

    def failed(self, failure: Any) -> None:
        source = failure.request.meta["source_key"]
        response = getattr(failure.value, "response", None)
        status = f" HTTP {response.status}" if response is not None else ""
        self.state.error(source, f"request failed: {type(failure.value).__name__}{status}")

    def parse_page(self, response: Response, source: Source):
        source = source_for_page(source, response.url)
        if source.key == "hm" and not re.search(
            source.product_pattern, response.url, re.IGNORECASE
        ):
            records = extract_hm_cards(response, source, self.limit)
            self.state.discovered[source.key] = self.state.discovered.get(source.key, 0) + len(
                records
            )
            self.state.records.setdefault(source.key, []).extend(records)
            if not records:
                self.state.error(source.key, "no complete HUF product cards on category page")
            return
        try:
            record = extract_product(response, source)
        except ValueError:
            record = None
        if record is not None:
            self.state.discovered[source.key] = self.state.discovered.get(source.key, 0) + 1
            self.state.records.setdefault(source.key, []).append(record)
            return
        links = product_links(response, source, self.limit)
        self.state.discovered[source.key] = self.state.discovered.get(source.key, 0) + len(links)
        if not links:
            self.state.error(source.key, "no product records or links found on starting page")
            return
        for url in links:
            yield scrapy.Request(
                url,
                callback=self.parse_product,
                errback=self.failed,
                cb_kwargs={"source": source},
                meta={"source_key": source.key},
            )

    def parse_product(self, response: Response, source: Source) -> None:
        try:
            self.state.records.setdefault(source.key, []).append(extract_product(response, source))
        except (ValueError, TypeError) as exc:
            self.state.error(source.key, f"product extraction failed: {exc}")


def crawl(
    sources: list[Source], limit: int, overrides: dict[str, list[str]] | None = None
) -> CrawlState:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    state = CrawlState()
    process = CrawlerProcess(
        settings={
            "BOT_NAME": "fashion_finder_data",
            "USER_AGENT": "FashionFinderResearch/0.1 (+local bounded collection)",
            "ROBOTSTXT_OBEY": True,
            "DOWNLOAD_DELAY": 10,
            "DOWNLOAD_DELAY_JITTER": 0,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
            "DOWNLOAD_TIMEOUT": 20,
            "RETRY_TIMES": 1,
            "LOG_LEVEL": "INFO",
            "COOKIES_ENABLED": False,
            "TELNETCONSOLE_ENABLED": False,
            "REMOTE_CONTROL_ENABLED": False,
        }
    )
    process.crawl(ProductSpider, sources=sources, state=state, limit=limit, overrides=overrides)
    process.start()
    for source in sources:
        if not state.records.get(source.key) and not state.errors.get(source.key):
            state.error(
                source.key, "no records; robots policy or storefront access may block collection"
            )
    return state

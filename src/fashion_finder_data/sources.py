"""Configured Hungarian source hosts and supported collection methods."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

SourceKey = Literal[
    "zara",
    "hm",
    "reserved",
    "sinsay",
    "mohito",
    "house",
    "cropp",
    "pullandbear",
    "aboutyou",
    "answear",
    "demo",
]


@dataclass(frozen=True)
class Source:
    key: str
    host: str
    category_url: str | None
    category: str | None
    product_pattern: str | None
    card_selector: str | None = None
    market_paths: tuple[str, ...] = ("/hu/", "/hu_hu/")
    # "lpp": category pages embed their product list (LPP group storefronts).
    listing: str | None = None

    @property
    def crawl_enabled(self) -> bool:
        return self.category_url is not None and self.product_pattern is not None


def lpp_source(key: str, host: str, category_url: str, category: str | None) -> Source:
    """Reserved, Sinsay, Mohito, House and Cropp share one storefront platform."""
    return Source(
        key,
        host,
        category_url,
        category,
        r"/hu/hu/[^/]+-[a-z0-9]{3,}-[a-z0-9]{2,}$",
        "article.es-product a.es-product-photo::attr(href)",
        listing="lpp",
    )


# Feed-only sources have no invented category URL or unverified page parser.
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
    "reserved": lpp_source(
        "reserved", "reserved.com", "https://www.reserved.com/hu/hu/noi/ruhazat/kabatok", "coats"
    ),
    "sinsay": lpp_source(
        "sinsay", "sinsay.com", "https://www.sinsay.com/hu/hu/noi/ruhazat/dzsekik", "jackets"
    ),
    "mohito": lpp_source(
        "mohito", "mohito.com", "https://www.mohito.com/hu/hu/ruhazat/kabatok", "coats"
    ),
    "house": lpp_source(
        "house",
        "housebrand.com",
        "https://www.housebrand.com/hu/hu/noi/ruhak/kabatok-dzsekik",
        None,
    ),
    "cropp": lpp_source(
        "cropp", "cropp.com", "https://www.cropp.com/hu/hu/noi/ruhazat/dzsekik-kabatok", None
    ),
    "pullandbear": Source(
        "pullandbear",
        "pullandbear.com",
        "https://www.pullandbear.com/hu/noi/ruhazat/bluzok-es-ingek-n6525",
        "shirts",
        r"-p\d+\.html(?:$|\?)",
    ),
    "aboutyou": Source("aboutyou", "aboutyou.hu", None, None, None, market_paths=("/p/",)),
    "answear": Source("answear", "answear.hu", None, None, None, market_paths=("/p/",)),
    "demo": Source("demo", "fashion-finder-demo.invalid", None, None, None),
}

CRAWL_SOURCES = {key: source for key, source in SOURCES.items() if source.crawl_enabled}
PRODUCTION_SOURCES = {key: source for key, source in SOURCES.items() if key != "demo"}


def allowed_url(source: Source, value: str) -> bool:
    """Accept direct HTTPS product URLs on one configured Hungarian storefront."""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if source.key in {"aboutyou", "answear"} and host not in {
        source.host,
        "www." + source.host,
    }:
        return False
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or port not in (None, 443)
        or not (host == source.host or host.endswith("." + source.host))
    ):
        return False
    return any(
        parts.path.startswith(prefix) and len(parts.path) > len(prefix)
        for prefix in source.market_paths
    )


def is_hungarian_source_url(source_key: str, value: str) -> bool:
    source = SOURCES.get(source_key)
    return source is not None and allowed_url(source, value)

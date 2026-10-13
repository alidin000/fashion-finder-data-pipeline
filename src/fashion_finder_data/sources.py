"""Configured Hungarian source hosts and supported collection methods."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Source:
    key: str
    host: str
    category_url: str | None
    category: str | None
    product_pattern: str | None
    card_selector: str | None = None
    market_paths: tuple[str, ...] = ("/hu/", "/hu_hu/")

    @property
    def crawl_enabled(self) -> bool:
        return self.category_url is not None and self.product_pattern is not None


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
    "aboutyou": Source("aboutyou", "aboutyou.hu", None, None, None, market_paths=("/p/",)),
    "answear": Source("answear", "answear.hu", None, None, None, market_paths=("/p/",)),
}

CRAWL_SOURCES = {key: source for key, source in SOURCES.items() if source.crawl_enabled}


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

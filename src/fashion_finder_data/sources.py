"""Configured Hungarian source hosts and supported collection methods.

Source definitions live in ``source_definitions.toml`` next to this module, so adding a
shop that publishes structured data is a configuration change. This module validates
that file once at import time and exposes the same ``Source`` objects as before.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

from . import __version__

DEFINITIONS_PATH = Path(__file__).with_name("source_definitions.toml")

# The product token robots.txt groups are matched against (RFC 9309 section 2.2.1).
ROBOTS_TOKEN = "FashionFinderBot"
# Where a site operator can learn what the crawler does and how to reach the owner.
# Set FASHION_FINDER_CONTACT_URL to the project's public contact page once it exists.
DEFAULT_CONTACT_URL = "https://github.com/alidin000/fashion-finder-data-pipeline"

STATUSES = ("reviewed", "blocked", "candidate", "feed-only", "demo", "practice")
# Never a retailer: the synthetic demo and the public-dataset practice catalogue.
NON_RETAIL_STATUSES = frozenset({"demo", "practice"})
LISTINGS = ("lpp", "hm-cards")
PAGINATION_TYPES = ("lpp", "query", "rel-next")
EXTRACTION_KEYS = {"type_aliases", "title_suffix", "listing_structured_data", "state", "brand"}
REFRESH_KEYS = {"strategy", "interval_hours"}
REFRESH_STRATEGIES = ("product", "listing")
RENDERINGS = ("http", "browser")
DEFAULT_INTERVAL_HOURS = 12.0
REMOVAL_KEYS = {
    "web_evidence",
    "missing_from_sample",
    "http_403_429_5xx",
    "http_404_410",
}
FIELDS = {
    "name",
    "host",
    "hosts",
    "status",
    "platform",
    "category_url",
    "category",
    "product_pattern",
    "card_selector",
    "market_paths",
    "listing",
    "category_urls",
    "sitemaps",
    "sitemap_follow",
    "notes",
    "pagination",
    "category_paths",
    "slug_categories",
    "extraction",
    "removal",
    "refresh",
    "rendering",
    "max_concurrency",
}
_KEY = re.compile(r"[a-z][a-z0-9_]{1,31}\Z")

PLATFORMS: dict[str, dict[str, Any]] = {
    # Reserved, Sinsay, Mohito, House and Cropp share one storefront platform whose
    # category pages embed their product list (`window.getCatalogData`).
    "lpp": {
        "product_pattern": r"/hu/hu/[^/]+-[a-z0-9]{3,}-[a-z0-9]{2,}$",
        "card_selector": "article.es-product a.es-product-photo::attr(href)",
        "listing": "lpp",
        "pagination": {"type": "lpp"},
        # One cumulative category page re-observes up to ~1,000 offers, so refreshing
        # through the stored listing is ~100x cheaper than one request per product.
        "refresh": {"strategy": "listing"},
    },
}


def contact_url(value: str | None = None) -> str:
    """The contact URL announced in the User-Agent; refuse values that could mislead."""
    url = (value or os.environ.get("FASHION_FINDER_CONTACT_URL") or DEFAULT_CONTACT_URL).strip()
    parts = urlsplit(url)
    valid = (parts.scheme == "https" and bool(parts.hostname) and not parts.username) or (
        parts.scheme == "mailto" and "@" in parts.path
    )
    if not valid or any(char in url for char in "()\r\n; "):
        raise ValueError("contact URL must be an https:// or mailto: address")
    return url


def user_agent(contact: str | None = None) -> str:
    """An honest User-Agent: the project's bot token, version and a contact URL."""
    return f"{ROBOTS_TOKEN}/{__version__} (+{contact_url(contact)})"


USER_AGENT = user_agent(DEFAULT_CONTACT_URL)


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
    # "hm-cards": complete offers on H&M category cards.
    listing: str | None = None
    name: str = ""
    status: str = "reviewed"
    # Exact allowed hostnames; empty means `host` and any of its subdomains.
    hosts: tuple[str, ...] = ()
    # Extra curated category pages, used by `collect --all-categories`.
    category_urls: tuple[str, ...] = ()
    # Sitemap or sitemap-index URLs for product discovery.
    sitemaps: tuple[str, ...] = ()
    # Regular expression a child sitemap URL must match to be followed.
    sitemap_follow: str | None = None
    notes: str = ""
    pagination: Mapping[str, Any] = field(default_factory=dict, compare=False)
    # URL path prefix -> category key, checked before the slug rules.
    category_paths: Mapping[str, str] = field(default_factory=dict, compare=False)
    # URL slug -> category key; an empty string forces "unclassified".
    slug_categories: Mapping[str, str] = field(default_factory=dict, compare=False)
    extraction: Mapping[str, Any] = field(default_factory=dict, compare=False)
    removal: Mapping[str, Any] = field(default_factory=dict, compare=False)
    # "product": refresh re-fetches each offer's page; "listing": its category page.
    refresh: Mapping[str, Any] = field(default_factory=dict, compare=False)
    # "browser" sources render in a separate, low-concurrency Playwright crawler.
    rendering: str = "http"
    # Concurrent requests per host; the request spacing rule still applies to each start.
    max_concurrency: int = 1

    @property
    def refresh_strategy(self) -> str:
        return self.refresh.get("strategy", "product")

    @property
    def interval_hours(self) -> float:
        return float(self.refresh.get("interval_hours", DEFAULT_INTERVAL_HOURS))

    @property
    def crawl_enabled(self) -> bool:
        return (
            self.status != "feed-only"
            and self.status not in NON_RETAIL_STATUSES
            and (self.product_pattern is not None or bool(self.sitemaps))
        )

    @property
    def collect_by_default(self) -> bool:
        """Only reviewed sources run in `collect all`; others need an explicit request."""
        return self.crawl_enabled and self.status == "reviewed"

    @property
    def display_name(self) -> str:
        return self.name or self.key


def lpp_source(key: str, host: str, category_url: str, category: str | None) -> Source:
    """Reserved, Sinsay, Mohito, House and Cropp share one storefront platform."""
    preset = PLATFORMS["lpp"]
    return Source(
        key,
        host,
        category_url,
        category,
        preset["product_pattern"],
        preset["card_selector"],
        listing="lpp",
        pagination=MappingProxyType(dict(preset["pagination"])),
        refresh=MappingProxyType(dict(preset["refresh"])),
    )


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    return tuple(value)


def _https(url: Any, name: str) -> str:
    parts = urlsplit(url if isinstance(url, str) else "")
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"{name} must be an https:// URL: {url!r}")
    return url


def _source(key: str, raw: dict[str, Any]) -> Source:
    if not _KEY.fullmatch(key):
        raise ValueError(f"invalid source key {key!r}")
    unknown = set(raw) - FIELDS
    if unknown:
        raise ValueError(f"{key}: unknown fields {sorted(unknown)}")
    platform = raw.get("platform")
    if platform is not None and platform not in PLATFORMS:
        raise ValueError(f"{key}: unknown platform {platform!r}")
    values = {**(PLATFORMS[platform] if platform else {}), **raw}
    host = values.get("host")
    if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
        raise ValueError(f"{key}: host must be a lowercase domain name")
    status = values.get("status", "reviewed")
    if status not in STATUSES:
        raise ValueError(f"{key}: status must be one of {STATUSES}")
    listing = values.get("listing")
    if listing is not None and listing not in LISTINGS:
        raise ValueError(f"{key}: listing must be one of {LISTINGS}")
    for pattern_key in ("product_pattern", "sitemap_follow"):
        if values.get(pattern_key) is not None:
            re.compile(values[pattern_key])
    pagination = dict(values.get("pagination") or {})
    if pagination and pagination.get("type") not in PAGINATION_TYPES:
        raise ValueError(f"{key}: pagination type must be one of {PAGINATION_TYPES}")
    extraction = dict(values.get("extraction") or {})
    if set(extraction) - EXTRACTION_KEYS:
        raise ValueError(f"{key}: unknown extraction fields {sorted(extraction)}")
    if extraction.get("title_suffix"):
        re.compile(extraction["title_suffix"])
    removal = dict(values.get("removal") or {})
    if set(removal) - REMOVAL_KEYS:
        raise ValueError(f"{key}: unknown removal fields {sorted(removal)}")
    refresh = dict(values.get("refresh") or {})
    if set(refresh) - REFRESH_KEYS:
        raise ValueError(f"{key}: unknown refresh fields {sorted(refresh)}")
    if refresh.get("strategy", "product") not in REFRESH_STRATEGIES:
        raise ValueError(f"{key}: refresh strategy must be one of {REFRESH_STRATEGIES}")
    if not 1 <= float(refresh.get("interval_hours", DEFAULT_INTERVAL_HOURS)) <= 24 * 30:
        raise ValueError(f"{key}: refresh interval must be between 1 hour and 30 days")
    rendering = values.get("rendering", "http")
    if rendering not in RENDERINGS:
        raise ValueError(f"{key}: rendering must be one of {RENDERINGS}")
    concurrency = values.get("max_concurrency", 1)
    if not isinstance(concurrency, int) or not 1 <= concurrency <= 4:
        raise ValueError(f"{key}: max_concurrency must be an integer from 1 to 4")
    source = Source(
        key=key,
        host=host,
        category_url=values.get("category_url"),
        category=values.get("category"),
        product_pattern=values.get("product_pattern"),
        card_selector=values.get("card_selector"),
        market_paths=_strings(values.get("market_paths", ["/hu/", "/hu_hu/"]), "market_paths"),
        listing=listing,
        name=values.get("name", ""),
        status=status,
        hosts=_strings(values.get("hosts", []), "hosts"),
        category_urls=_strings(values.get("category_urls", []), "category_urls"),
        sitemaps=_strings(values.get("sitemaps", []), "sitemaps"),
        sitemap_follow=values.get("sitemap_follow"),
        notes=values.get("notes", ""),
        pagination=MappingProxyType(pagination),
        category_paths=MappingProxyType(dict(values.get("category_paths") or {})),
        slug_categories=MappingProxyType(dict(values.get("slug_categories") or {})),
        extraction=MappingProxyType(extraction),
        removal=MappingProxyType(removal),
        refresh=MappingProxyType(refresh),
        rendering=rendering,
        max_concurrency=concurrency,
    )
    for url in (*([source.category_url] if source.category_url else []), *source.category_urls):
        if not allowed_url(source, _https(url, "category URL")):
            raise ValueError(f"{key}: category URL {url!r} is outside the storefront")
    for url in source.sitemaps:
        if not _host_allowed(source, urlsplit(_https(url, "sitemap")).hostname, sitemap=True):
            raise ValueError(f"{key}: sitemap {url!r} is not on the shop's own domain")
    return source


def load_definitions(path: Path = DEFINITIONS_PATH) -> dict[str, Source]:
    """Read and validate every source definition; a bad file fails loudly."""
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    if set(document) != {"sources"} or not isinstance(document["sources"], dict):
        raise ValueError("source definitions need exactly one [sources] table")
    return {key: _source(key, raw) for key, raw in document["sources"].items()}


def _host_allowed(source: Source, host: str | None, *, sitemap: bool = False) -> bool:
    """Product pages must be on the listed hosts; sitemaps and photos may use subdomains."""
    host = (host or "").lower()
    if source.hosts and not sitemap:
        return host in source.hosts
    return host == source.host or host.endswith("." + source.host)


def allowed_url(source: Source, value: str) -> bool:
    """Accept direct HTTPS product URLs on one configured Hungarian storefront."""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return False
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or port not in (None, 443)
        or not _host_allowed(source, parts.hostname)
    ):
        return False
    return any(
        parts.path.startswith(prefix) and len(parts.path) > len(prefix)
        for prefix in source.market_paths
    )


SOURCES: dict[str, Source] = load_definitions()

# Pydantic models validate source keys against this literal, built from the definitions.
SourceKey = Literal[tuple(SOURCES)]  # type: ignore[valid-type]

CRAWL_SOURCES = {key: source for key, source in SOURCES.items() if source.crawl_enabled}
DEFAULT_COLLECT_SOURCES = {
    key: source for key, source in CRAWL_SOURCES.items() if source.collect_by_default
}
NON_RETAIL_SOURCES = frozenset(
    key for key, source in SOURCES.items() if source.status in NON_RETAIL_STATUSES
)
PRODUCTION_SOURCES = {
    key: source for key, source in SOURCES.items() if key not in NON_RETAIL_SOURCES
}


def source_for_host(host: str | None) -> Source | None:
    """The configured source that owns a hostname, including sitemap and image hosts."""
    matches = [source for source in SOURCES.values() if _host_allowed(source, host, sitemap=True)]
    return max(matches, key=lambda source: len(source.host)) if matches else None


def is_hungarian_source_url(source_key: str, value: str) -> bool:
    source = SOURCES.get(source_key)
    return source is not None and allowed_url(source, value)

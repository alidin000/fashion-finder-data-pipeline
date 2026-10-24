"""Sitemap discovery, listing pagination and incremental refresh decisions.

Pure functions only: the spider feeds them responses and crawl-state rows, and
they decide which URLs deserve one of the run's limited requests.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from scrapy.utils.gz import gunzip
from scrapy.utils.sitemap import Sitemap

from .crawl_state import instant, product_key

MAX_SITEMAP_BYTES = 50 * 1024 * 1024  # the sitemaps.org limit for one uncompressed file

# Refresh priorities: higher runs first. Equal priorities keep the oldest check first.
PRIORITY_CHANGED_UPSTREAM = 100  # sitemap lastmod is newer than our last check
PRIORITY_MISSING = 90  # missing from a complete listing or answered 404/410 once
PRIORITY_STALE_IN_STOCK = 80  # stale price or stock for an offer shoppers may see
PRIORITY_NEW = 60  # never collected
PRIORITY_STALE_OTHER = 40  # stale, but out of stock or unknown
PRIORITY_RECHECK_GONE = 10  # already answered 404/410 twice; confirm occasionally


@dataclass(frozen=True)
class SitemapEntry:
    url: str
    lastmod: datetime | None
    is_sitemap: bool


def parse_lastmod(value: str | None) -> datetime | None:
    """W3C datetime from a sitemap (``2026-10-13``, ``2026-10-13T10:00:00+02:00``)."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def sitemap_body(body: bytes, url: str = "") -> bytes:
    """Decompress a gzip sitemap (``.xml.gz`` or a gzip body) within the 50 MB limit."""
    if body[:2] == b"\x1f\x8b":
        return gunzip(body, max_size=MAX_SITEMAP_BYTES)
    if len(body) > MAX_SITEMAP_BYTES:
        raise ValueError("sitemap exceeds 50 MB")
    return body


def parse_sitemap(body: bytes, base_url: str) -> list[SitemapEntry]:
    """Entries of a sitemap or sitemap index; external entities are never resolved."""
    data = sitemap_body(body, base_url)
    try:
        sitemap = Sitemap(data)
    except (StopIteration, SyntaxError, ValueError):
        raise ValueError("not a sitemap XML document") from None
    if sitemap.type not in {"urlset", "sitemapindex"}:
        raise ValueError(f"unexpected sitemap root <{sitemap.type}>")
    is_index = sitemap.type == "sitemapindex"
    entries = []
    for item in sitemap:
        loc = (item.get("loc") or "").strip()
        if not loc:
            continue
        entries.append(
            SitemapEntry(urljoin(base_url, loc), parse_lastmod(item.get("lastmod")), is_index)
        )
    return entries


def sitemaps_from_robots(text: str) -> list[str]:
    """``Sitemap:`` lines of a robots.txt body, in order."""
    return [match[1].strip() for match in re.finditer(r"(?im)^\s*sitemap\s*:\s*(\S+)\s*$", text)]


def choose_child_sitemaps(
    entries: Iterable[SitemapEntry],
    follow: str | None,
    fetched: Mapping[str, datetime | None],
    limit: int,
) -> list[SitemapEntry]:
    """Child sitemaps to fetch this run: changed or never fetched first, then oldest fetched.

    ``fetched`` maps a sitemap URL to when this collector last fetched it. Rotating
    through the oldest keeps successive small runs covering every child file.
    """
    pattern = re.compile(follow, re.IGNORECASE) if follow else None
    candidates = [entry for entry in entries if pattern is None or pattern.search(entry.url)]
    floor = datetime.min.replace(tzinfo=UTC)

    def order(entry: SitemapEntry) -> tuple[int, datetime]:
        last = fetched.get(entry.url)
        if last is None:
            return (0, floor)
        if entry.lastmod is not None and entry.lastmod > last:
            return (0, last)
        return (1, last)

    return sorted(candidates, key=order)[:limit]


@dataclass(frozen=True)
class RefreshCandidate:
    url: str
    priority: int
    reason: str
    lastmod: datetime | None = None
    last_checked: datetime | None = None


def refresh_decision(
    url: str,
    known: Mapping[str, Any] | None,
    *,
    lastmod: datetime | None,
    now: datetime,
    stale_after: timedelta,
    include_new: bool = True,
) -> RefreshCandidate | None:
    """Whether and how urgently to fetch one product URL; None means skip it this run."""
    if known is None:
        return RefreshCandidate(url, PRIORITY_NEW, "new", lastmod) if include_new else None
    checked = instant(known.get("last_checked") or known.get("last_seen"))
    state = known.get("state") or "active"
    if state == "expired":
        return None
    if state == "gone":
        if (known.get("gone_hits") or 0) >= 2:
            return RefreshCandidate(url, PRIORITY_RECHECK_GONE, "recheck gone", lastmod, checked)
        return RefreshCandidate(url, PRIORITY_MISSING, "answered 404/410 once", lastmod, checked)
    if state == "missing":
        return RefreshCandidate(url, PRIORITY_MISSING, "missing from listing", lastmod, checked)
    if checked is None:
        return RefreshCandidate(url, PRIORITY_STALE_OTHER, "never checked", lastmod, None)
    if lastmod is not None and lastmod > checked:
        return RefreshCandidate(
            url, PRIORITY_CHANGED_UPSTREAM, "changed upstream", lastmod, checked
        )
    if now - checked >= stale_after:
        if known.get("availability") == "in_stock":
            return RefreshCandidate(url, PRIORITY_STALE_IN_STOCK, "stale", lastmod, checked)
        return RefreshCandidate(url, PRIORITY_STALE_OTHER, "stale", lastmod, checked)
    return None


def prioritise(
    urls: Iterable[tuple[str, datetime | None]],
    known: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(days=3),
    include_new: bool = True,
) -> list[RefreshCandidate]:
    """Order product URLs for a bounded run: urgent refreshes first, fresh ones skipped."""
    now = now or datetime.now(UTC)
    floor = datetime.min.replace(tzinfo=UTC)
    decided = []
    seen: set[str] = set()
    for url, lastmod in urls:
        key = product_key(url)
        if key in seen:
            continue
        seen.add(key)
        decision = refresh_decision(
            url,
            known.get(key),
            lastmod=lastmod,
            now=now,
            stale_after=stale_after,
            include_new=include_new,
        )
        if decision is not None:
            decided.append(decision)
    return sorted(
        decided,
        key=lambda item: (
            -item.priority,
            item.last_checked or floor,
            -(item.lastmod or floor).timestamp(),
        ),
    )


def without_page(url: str, param: str = "page") -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != param]
    return urlunsplit(parts._replace(query=urlencode(query)))


def with_page(url: str, page: int, param: str = "page") -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != param]
    return urlunsplit(parts._replace(query=urlencode([*query, (param, str(page))])))


def page_number(url: str, param: str = "page") -> int:
    value = dict(parse_qsl(urlsplit(url).query)).get(param)
    return int(value) if value and value.isdigit() else 1


def next_listing_page(
    url: str,
    pagination: Mapping[str, Any],
    *,
    rel_next: str | None,
    found_products: bool,
) -> str | None:
    """The next listing page under the source's pagination rule, or None when done."""
    kind = pagination.get("type")
    max_pages = int(pagination.get("max_pages", 50))
    if not found_products:
        return None
    if kind == "rel-next" and rel_next:
        candidate = urljoin(url, rel_next)
        param = pagination.get("param", "page")
        return candidate if page_number(candidate, param) <= max_pages else None
    if kind == "query":
        param = pagination.get("param", "page")
        current = page_number(url, param)
        return with_page(url, current + 1, param) if current < max_pages else None
    return None

"""Download product photos of stored offers for the local image index.

Files and a manifest land under one directory. The manifest is the input that
``index-images`` expects, so fetching and indexing are two separate steps. Only
photos the index would accept are fetched, and every request respects robots.txt
(including its Crawl-delay) and the text-and-data-mining opt-outs of EU DSM
Directive Article 4 (TDMRep, the ``tdm-reservation`` header, ``noai`` robots tags).

Throughput within politeness: each host keeps its own spacing, and downloads are
interleaved across hosts so waits overlap; the best photos of each offer are
chosen (packshots first, no size charts or logos); files already on disk are
reused, optionally revalidated with ETag/Last-Modified, and identical bytes are
stored once.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError
from protego import Protego

from .image_index import MAX_ENTRIES, MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, image_skip_reason
from .image_selection import select_photos
from .public_catalogue import _safe_image_url
from .source_policy import SourcePolicy
from .sources import ROBOTS_TOKEN, SOURCES, USER_AGENT
from .storage import Database, offer_rows

MANIFEST_FILE = "manifest.json"
EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}

Fetched = tuple[bytes, Mapping[str, str]]
Fetch = Callable[..., Fetched]
CACHE_FILE = "image-cache.json"


def _site(host: str | None) -> str:
    return ".".join((host or "").lower().split(".")[-2:])


class _SameSiteRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to HTTPS on the same site; the checks ran on the first URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if new.scheme != "https" or _site(new.hostname) != _site(old.hostname):
            raise urllib.error.HTTPError(newurl, code, "redirect leaves the site", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameSiteRedirects)


def http_get(
    url: str, headers: Mapping[str, str] | None = None, *, agent: str = USER_AGENT
) -> Fetched:
    request = urllib.request.Request(url, headers={"User-Agent": agent, **(headers or {})})
    with _OPENER.open(request, timeout=20) as response:
        data = response.read(MAX_IMAGE_BYTES + 1)
        headers = {key.lower(): value for key, value in response.headers.items()}
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("response exceeds 5 MB")
    return data, headers


class Throttle:
    """Space requests per host by ``delay`` or the host's robots.txt Crawl-delay if longer.

    The first request to a host goes at once; later ones wait only for that host,
    so photos from several shops download in parallel time without any one shop
    seeing more than one request per allowed interval.
    """

    def __init__(
        self,
        fetch: Fetch,
        delay: float,
        sleep: Callable[[float], None],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._fetch = fetch
        self._delay = delay
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}
        self.crawl_delays: dict[str, float] = {}
        self.requests = 0

    def wait_for(self, url: str) -> float:
        host = (urlsplit(url).hostname or "").lower()
        last = self._last.get(host)
        if last is None:
            return 0.0
        spacing = max(self._delay, self.crawl_delays.get(host, 0.0))
        return max(0.0, last + spacing - self._clock())

    def __call__(self, url: str, headers: Mapping[str, str] | None = None) -> Fetched:
        wait = self.wait_for(url)
        if wait > 0:
            self._sleep(wait)
        self._last[(urlsplit(url).hostname or "").lower()] = self._clock()
        self.requests += 1
        return self._fetch(url, headers) if headers else self._fetch(url)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class RobotsRules:
    """One robots.txt per host, parsed with Protego as Scrapy does (RFC 9309)."""

    def __init__(self, fetch: Fetch, throttle: Throttle | None = None):
        self._fetch = fetch
        self._throttle = throttle
        self._parsers: dict[str, Protego | bool] = {}

    def allowed(self, url: str) -> bool:
        origin = _origin(url)
        if origin not in self._parsers:
            rules: Protego | bool
            try:
                body, _ = self._fetch(origin + "/robots.txt")
                rules = Protego.parse(body.decode("utf-8", "replace"))
                delay = rules.crawl_delay(ROBOTS_TOKEN)
                if delay is not None and self._throttle is not None:
                    host = (urlsplit(origin).hostname or "").lower()
                    self._throttle.crawl_delays[host] = float(delay)
            except urllib.error.HTTPError as exc:
                # 401/403 forbid everything; another 4xx means there are no rules.
                rules = exc.code not in (401, 403) and 400 <= exc.code < 500
            except (OSError, ValueError):
                rules = False  # unreadable rules: skip the host rather than guess
            self._parsers[origin] = rules
        rules = self._parsers[origin]
        return rules if isinstance(rules, bool) else rules.can_fetch(url, ROBOTS_TOKEN)


def _location_matches(pattern: str, path: str) -> bool:
    pattern = pattern if pattern.startswith("/") else "/" + pattern
    return re.match(re.escape(pattern).replace(r"\*", ".*"), path) is not None


class TdmRules:
    """Text-and-data-mining reservations published as /.well-known/tdmrep.json."""

    def __init__(self, fetch: Fetch):
        self._fetch = fetch
        self._rules: dict[str, list | None] = {}

    def reserved(self, url: str) -> bool | None:
        """True if mining is reserved, False if not, None if the rules are unreadable."""
        origin = _origin(url)
        if origin not in self._rules:
            rules: list | None
            try:
                body, _ = self._fetch(origin + "/.well-known/tdmrep.json")
                parsed = json.loads(body)
                rules = parsed if isinstance(parsed, list) else None  # malformed: unreadable
            except urllib.error.HTTPError as exc:
                # No file means no reservation. Several LPP shops answer 301 without a
                # Location (and a 404 body): a redirect that leads nowhere is "no file"
                # too, not "unreadable" (docs/data-research/lpp-catalogue-capacity.md).
                no_file = 400 <= exc.code < 500 and exc.code not in (401, 403, 429)
                rules = [] if no_file or 300 <= exc.code < 400 else None
            except (OSError, ValueError):
                rules = None
            self._rules[origin] = rules
        rules = self._rules[origin]
        if rules is None:
            return None
        path = urlsplit(url).path or "/"
        for rule in rules:  # the first matching location decides
            if isinstance(rule, dict) and _location_matches(str(rule.get("location", "")), path):
                return str(rule.get("tdm-reservation")) == "1"
        return False


def retailer_image_url(source_key: str, url: str) -> bool:
    """Accept HTTPS images on the retailer's own domains, such as ``static.sinsay.com``."""
    source = SOURCES.get(source_key)
    host = (urlsplit(url).hostname or "").lower() if _safe_image_url(url) else ""
    return source is not None and (host == source.host or host.endswith("." + source.host))


def checked_image(data: bytes) -> Image.Image:
    """The decoded photo; ValueError for another encoding, too many pixels or broken data."""
    try:
        image = Image.open(BytesIO(data))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("cannot decode image") from exc
    try:
        if image.format not in EXTENSIONS:
            raise ValueError("unsupported image encoding")
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("image dimensions are too large")
        image.load()
    except (OSError, Image.DecompressionBombError) as exc:
        image.close()
        raise ValueError("cannot decode image") from exc
    except ValueError:
        image.close()
        raise
    return image


def _image_extension(data: bytes) -> str:
    with checked_image(data) as image:
        return EXTENSIONS[image.format]


def _kept_entries(
    path: Path,
    root: Path,
    offers: dict[int, dict[str, Any]],
    policies: dict[tuple[str, str], SourcePolicy],
    now: datetime,
) -> list[dict]:
    """Keep earlier entries whose file exists and that the index would still accept."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    kept = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            offer = offers.get(entry["offer_id"])
            exists = (root / entry["image_path"]).is_file()
            reason = image_skip_reason(offer, entry["image_url"], policies, now)
        except (KeyError, TypeError):
            continue
        if exists and reason is None:
            kept.append(entry)
    return kept


def in_turn[T](items: Iterable[T], group: Callable[[T], str]) -> list[T]:
    """Items round-robin across their groups, keeping each group's own order."""
    queues: dict[str, deque[T]] = {}
    for item in items:
        queues.setdefault(group(item), deque()).append(item)
    ordered: list[T] = []
    while queues:
        for key in list(queues):
            ordered.append(queues[key].popleft())
            if not queues[key]:
                del queues[key]
    return ordered


def _offers_by_source_in_turn(offers: dict[int, dict[str, Any]]) -> list[tuple[int, dict]]:
    """Offers in id order within each shop, taking shops in turn.

    Offer ids follow collection order, so without this a ``--limit`` over every shop would
    spend the whole budget on the shop collected first.
    """
    return in_turn(sorted(offers.items()), lambda pair: pair[1]["source_key"])


def _interleave(planned: list[tuple]) -> list[tuple]:
    """Round-robin across image hosts so one host's spacing never idles the others."""
    return in_turn(planned, lambda item: (urlsplit(item[1]).hostname or "").lower())


def _load_cache(root: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads((root / CACHE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_cache(root: Path, cache: dict[str, dict[str, Any]]) -> None:
    temporary = root / f".{CACHE_FILE}.tmp"
    temporary.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(root / CACHE_FILE)


def _opted_out(headers: Mapping[str, str]) -> str | None:
    if headers.get("tdm-reservation", "").strip() == "1":
        return "text-and-data-mining reserved (header)"
    tags = {tag.strip().lower() for tag in headers.get("x-robots-tag", "").split(",")}
    if tags & {"noai", "noimageai"}:
        return "image opts out of AI use (X-Robots-Tag)"
    return None


class PhotoRefused(Exception):
    """A photo that was not downloaded; ``str()`` is the reason reports count.

    ``opted_out`` marks the shop's own opt-outs (robots.txt, TDMRep, opt-out headers),
    as opposed to failures such as an HTTP error.
    """

    def __init__(self, reason: str, *, opted_out: bool = False):
        super().__init__(reason)
        self.opted_out = opted_out


class PolitePhotoFetcher:
    """The checks every photo download makes, shared by fetch-images and index-photos.

    Before a download: the image host's robots.txt (read once, with its Crawl-delay)
    and TDMRep on both the image host and the shop's own site. The download itself is
    spaced per host (``Throttle``), may be conditional (ETag / Last-Modified), and is
    refused if the response opts out of mining (``tdm-reservation: 1`` or an
    ``X-Robots-Tag`` with ``noai``/``noimageai``) or exceeds 5 MB.
    """

    def __init__(
        self,
        fetch: Fetch = http_get,
        delay: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.throttle = Throttle(fetch, delay, sleep, clock)
        self.robots = RobotsRules(self.throttle, self.throttle)
        self.tdm = TdmRules(self.throttle)
        self.photo_requests = 0

    @property
    def requests(self) -> int:
        """Every request so far: robots.txt, TDMRep files and photos."""
        return self.throttle.requests

    @property
    def crawl_delays(self) -> dict[str, float]:
        return self.throttle.crawl_delays

    def wait_for(self, url: str) -> float:
        return self.throttle.wait_for(url)

    def refusal(self, source_key: str, url: str) -> str | None:
        """Why the shop's published rules forbid fetching this photo, or None."""
        if not self.robots.allowed(url):
            return "disallowed or unreadable robots.txt"
        # The shop's own site may reserve mining for everything it publishes.
        site = f"https://www.{SOURCES[source_key].host}/"
        reservations = [self.tdm.reserved(url), self.tdm.reserved(site)]
        if True in reservations:
            return "text-and-data-mining reserved (TDMRep)"
        if None in reservations:
            return "unreadable TDMRep rules"
        return None

    def download(self, url: str, validators: Mapping[str, Any] | None = None) -> Fetched | None:
        """The photo's bytes and headers, or None for 304 Not Modified.

        ``validators`` holds a cached ``etag`` and ``last_modified``; they are sent as
        ``If-None-Match`` / ``If-Modified-Since``. Raises :class:`PhotoRefused`.
        """
        conditional = {}
        if validators and validators.get("etag"):
            conditional["If-None-Match"] = validators["etag"]
        if validators and validators.get("last_modified"):
            conditional["If-Modified-Since"] = validators["last_modified"]
        self.photo_requests += 1
        try:
            data, headers = self.throttle(url, conditional or None)
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and conditional:
                return None
            raise PhotoRefused(f"HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise PhotoRefused(str(exc) or type(exc).__name__) from None
        opted_out = _opted_out(headers)
        if opted_out:
            raise PhotoRefused(opted_out, opted_out=True)
        return data, headers


def fetch_images(
    database: Database,
    output_dir: Path,
    policies: dict[tuple[str, str], SourcePolicy],
    *,
    source: str | None = None,
    limit: int = 100,
    per_offer: int = 2,
    delay: float = 2.0,
    fetch: Fetch = http_get,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    revalidate_after_days: float | None = None,
) -> dict[str, Any]:
    """Download up to ``per_offer`` photos for each of ``limit`` eligible offers.

    ``revalidate_after_days`` re-checks photos fetched longer ago with a
    conditional request (ETag / Last-Modified); unchanged photos are not rewritten.
    """
    if not 1 <= limit <= MAX_ENTRIES:
        raise ValueError(f"limit must be between 1 and {MAX_ENTRIES}")
    if not 1 <= per_offer <= 8:
        raise ValueError("per-offer must be between 1 and 8")
    if not 0.5 <= delay <= 60:
        raise ValueError("delay must be between 0.5 and 60 seconds")
    if source is not None and source not in SOURCES:
        raise ValueError("unknown source")
    if revalidate_after_days is not None and not 0 < revalidate_after_days <= 365:
        raise ValueError("revalidate-after-days must be between 0 and 365")
    root = output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / MANIFEST_FILE
    now = datetime.now(UTC)
    offers = {
        row["id"]: row
        for row in offer_rows(database, status="active")
        if row["source_key"] != "demo"
    }
    # Earlier entries are pruned against all current offers, not only this source.
    entries = {
        (entry["offer_id"], entry["image_url"]): entry
        for entry in _kept_entries(manifest_path, root, offers, policies, now)
    }
    cache = _load_cache(root)
    by_hash = {
        item["sha256"]: item["path"]
        for item in cache.values()
        if isinstance(item, dict)
        and item.get("sha256")
        and (root / str(item.get("path"))).is_file()
    }

    def stale(url: str) -> bool:
        fetched = (cache.get(url) or {}).get("fetched_at")
        if revalidate_after_days is None:
            return False
        try:
            age = now - datetime.fromisoformat(fetched)
        except (TypeError, ValueError):
            return True
        return age.total_seconds() > revalidate_after_days * 86400

    counts: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    planned: list[tuple[dict[str, Any], str, bool]] = []
    selected = 0  # offers with at least one eligible photo
    for offer_id, offer in _offers_by_source_in_turn(offers):
        if source is not None and offer["source_key"] != source:
            continue
        if selected >= limit:
            break
        eligible = False
        for url in select_photos(json.loads(offer["image_urls"]), per_offer):
            reason = image_skip_reason(offer, url, policies, now)
            if reason:
                skipped[reason] += 1
                continue
            eligible = True
            if (offer_id, url) in entries and not stale(url):
                counts["reused"] += 1
            elif not retailer_image_url(offer["source_key"], url):
                failures["image URL is not on the retailer's HTTPS domains"] += 1
            else:
                planned.append((offer, url, (offer_id, url) in entries))
        selected += eligible
    if len(entries) + len(planned) > MAX_ENTRIES:
        raise ValueError(f"manifest would exceed {MAX_ENTRIES} images; use a smaller limit")
    fetcher = PolitePhotoFetcher(fetch, delay, sleep, clock)
    for offer, url, revalidate in _interleave(planned):
        stem = f"{offer['id']}-{hashlib.sha256(url.encode()).hexdigest()[:16]}"
        folder = root / offer["source_key"]
        existing = next(folder.glob(stem + ".*"), None) if folder.is_dir() else None
        if existing is not None and not revalidate:
            counts["reused"] += 1
        else:
            refusal = fetcher.refusal(offer["source_key"], url)
            if refusal:
                failures[refusal] += 1
                continue
            known = cache.get(url) or {}
            try:
                fetched = fetcher.download(url, known if existing is not None else None)
                if fetched is not None:
                    data, headers = fetched
                    extension = _image_extension(data)
            except (PhotoRefused, ValueError) as exc:
                failures[str(exc) or type(exc).__name__] += 1
                continue
            if fetched is None:
                counts["unchanged"] += 1
                cache[url] = {**known, "fetched_at": now.isoformat()}
            else:
                digest = hashlib.sha256(data).hexdigest()
                if existing is not None and known.get("sha256") == digest:
                    counts["unchanged"] += 1
                elif existing is None and digest in by_hash:
                    existing = root / by_hash[digest]  # the same photo under another URL
                    counts["deduplicated"] += 1
                else:
                    folder.mkdir(exist_ok=True)
                    existing = folder / f"{stem}.{extension}"
                    temporary = existing.with_name(f".{existing.name}.tmp")
                    temporary.write_bytes(data)
                    temporary.replace(existing)
                    counts["downloaded"] += 1
                path = existing.relative_to(root).as_posix()
                by_hash.setdefault(digest, path)
                cache[url] = {
                    "etag": headers.get("etag"),
                    "last_modified": headers.get("last-modified"),
                    "sha256": digest,
                    "path": path,
                    "fetched_at": now.isoformat(),
                }
        entries[(offer["id"], url)] = {
            "offer_id": offer["id"],
            "image_url": url,
            "image_path": existing.relative_to(root).as_posix(),
        }
    manifest = sorted(entries.values(), key=lambda item: (item["offer_id"], item["image_url"]))
    temporary = manifest_path.with_name(f".{MANIFEST_FILE}.tmp")
    temporary.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    _save_cache(root, cache)
    return {
        "status": "fetched" if not failures else "partial" if manifest else "failed",
        "offers_selected": selected,
        "downloaded": counts["downloaded"],
        "reused": counts["reused"],
        "unchanged_on_revalidation": counts["unchanged"],
        "deduplicated": counts["deduplicated"],
        "skipped_not_eligible": dict(skipped),
        "requests": fetcher.requests,
        "crawl_delays": fetcher.crawl_delays,
        "failures": dict(failures),
        "manifest_path": str(manifest_path),
        "manifest_entries": len(manifest),
        "retailer_rights_verified_by_tool": False,
    }

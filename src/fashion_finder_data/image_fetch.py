"""Download product photos of stored offers for the local image index.

Files and a manifest land under one directory. The manifest is the input that
``index-images`` expects, so fetching and indexing are two separate steps. Only
photos the index would accept are fetched, and every request respects robots.txt
and the text-and-data-mining opt-outs of EU DSM Directive Article 4 (TDMRep).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError
from protego import Protego

from .image_index import MAX_ENTRIES, MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, image_skip_reason
from .public_catalogue import _safe_image_url
from .source_policy import SourcePolicy
from .sources import SOURCES, USER_AGENT
from .storage import Database, offer_rows

MANIFEST_FILE = "manifest.json"
EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}

Fetched = tuple[bytes, Mapping[str, str]]
Fetch = Callable[[str], Fetched]


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


def http_get(url: str) -> Fetched:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _OPENER.open(request, timeout=20) as response:
        data = response.read(MAX_IMAGE_BYTES + 1)
        headers = {key.lower(): value for key, value in response.headers.items()}
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("response exceeds 5 MB")
    return data, headers


class Throttle:
    """Wait ``delay`` seconds before every request after the first."""

    def __init__(self, fetch: Fetch, delay: float, sleep: Callable[[float], None]):
        self._fetch = fetch
        self._delay = delay
        self._sleep = sleep
        self.requests = 0

    def __call__(self, url: str) -> Fetched:
        if self.requests:
            self._sleep(self._delay)
        self.requests += 1
        return self._fetch(url)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class RobotsRules:
    """One robots.txt per host, parsed with Protego as Scrapy does (RFC 9309)."""

    def __init__(self, fetch: Fetch):
        self._fetch = fetch
        self._parsers: dict[str, Protego | bool] = {}

    def allowed(self, url: str) -> bool:
        origin = _origin(url)
        if origin not in self._parsers:
            rules: Protego | bool
            try:
                body, _ = self._fetch(origin + "/robots.txt")
                rules = Protego.parse(body.decode("utf-8", "replace"))
            except urllib.error.HTTPError as exc:
                # 401/403 forbid everything; another 4xx means there are no rules.
                rules = exc.code not in (401, 403) and 400 <= exc.code < 500
            except (OSError, ValueError):
                rules = False  # unreadable rules: skip the host rather than guess
            self._parsers[origin] = rules
        rules = self._parsers[origin]
        return rules if isinstance(rules, bool) else rules.can_fetch(url, USER_AGENT)


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
                rules = [] if 400 <= exc.code < 500 else None
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


def _image_extension(data: bytes) -> str:
    try:
        with Image.open(BytesIO(data)) as image:
            extension = EXTENSIONS.get(image.format or "")
            if extension is None:
                raise ValueError("unsupported image encoding")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("image dimensions are too large")
            image.load()
            return extension
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("cannot decode image") from exc


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
) -> dict[str, Any]:
    """Download up to ``per_offer`` photos for each of ``limit`` eligible offers."""
    if not 1 <= limit <= MAX_ENTRIES:
        raise ValueError(f"limit must be between 1 and {MAX_ENTRIES}")
    if not 1 <= per_offer <= 8:
        raise ValueError("per-offer must be between 1 and 8")
    if not 0.5 <= delay <= 60:
        raise ValueError("delay must be between 0.5 and 60 seconds")
    if source is not None and source not in SOURCES:
        raise ValueError("unknown source")
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
    counts: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    planned: list[tuple[dict[str, Any], str]] = []
    selected = 0  # offers with at least one eligible photo
    for offer_id, offer in sorted(offers.items()):
        if source is not None and offer["source_key"] != source:
            continue
        if selected >= limit:
            break
        eligible = False
        for url in json.loads(offer["image_urls"])[:per_offer]:
            reason = image_skip_reason(offer, url, policies, now)
            if reason:
                skipped[reason] += 1
                continue
            eligible = True
            if (offer_id, url) in entries:
                counts["reused"] += 1
            elif not retailer_image_url(offer["source_key"], url):
                failures["image URL is not on the retailer's HTTPS domains"] += 1
            else:
                planned.append((offer, url))
        selected += eligible
    if len(entries) + len(planned) > MAX_ENTRIES:
        raise ValueError(f"manifest would exceed {MAX_ENTRIES} images; use a smaller limit")
    throttle = Throttle(fetch, delay, sleep)
    robots = RobotsRules(throttle)
    tdm = TdmRules(throttle)
    for offer, url in planned:
        stem = f"{offer['id']}-{hashlib.sha256(url.encode()).hexdigest()[:16]}"
        folder = root / offer["source_key"]
        existing = next(folder.glob(stem + ".*"), None) if folder.is_dir() else None
        if existing is None:
            # The shop's own site may reserve mining for everything it publishes.
            site = f"https://www.{SOURCES[offer['source_key']].host}/"
            if not robots.allowed(url):
                failures["disallowed or unreadable robots.txt"] += 1
                continue
            reservations = [tdm.reserved(url), tdm.reserved(site)]
            if True in reservations:
                failures["text-and-data-mining reserved (TDMRep)"] += 1
                continue
            if None in reservations:
                failures["unreadable TDMRep rules"] += 1
                continue
            try:
                data, headers = throttle(url)
                if headers.get("tdm-reservation", "").strip() == "1":
                    failures["text-and-data-mining reserved (header)"] += 1
                    continue
                extension = _image_extension(data)
            except urllib.error.HTTPError as exc:
                failures[f"HTTP {exc.code}"] += 1
                continue
            except (OSError, ValueError) as exc:
                failures[str(exc) or type(exc).__name__] += 1
                continue
            folder.mkdir(exist_ok=True)
            existing = folder / f"{stem}.{extension}"
            temporary = existing.with_name(f".{existing.name}.tmp")
            temporary.write_bytes(data)
            temporary.replace(existing)
            counts["downloaded"] += 1
        else:
            counts["reused"] += 1
        entries[(offer["id"], url)] = {
            "offer_id": offer["id"],
            "image_url": url,
            "image_path": existing.relative_to(root).as_posix(),
        }
    manifest = sorted(entries.values(), key=lambda item: (item["offer_id"], item["image_url"]))
    temporary = manifest_path.with_name(f".{MANIFEST_FILE}.tmp")
    temporary.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return {
        "status": "fetched" if not failures else "partial" if manifest else "failed",
        "offers_selected": selected,
        "downloaded": counts["downloaded"],
        "reused": counts["reused"],
        "skipped_not_eligible": dict(skipped),
        "requests": throttle.requests,
        "failures": dict(failures),
        "manifest_path": str(manifest_path),
        "manifest_entries": len(manifest),
        "retailer_rights_verified_by_tool": False,
    }

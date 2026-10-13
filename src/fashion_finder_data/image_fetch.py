"""Download product photos of stored offers for the local image index.

Files and a manifest land under one directory. The manifest is the input that
``index-images`` expects, so fetching and indexing are two separate steps.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from PIL import Image, UnidentifiedImageError

from .image_index import MAX_ENTRIES, MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS
from .public_catalogue import _safe_image_url
from .sources import SOURCES, USER_AGENT
from .storage import Database, offer_rows

MANIFEST_FILE = "manifest.json"
EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}

Fetch = Callable[[str], bytes]


def http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("response exceeds 5 MB")
    return data


class Throttle:
    """Wait ``delay`` seconds before every request after the first."""

    def __init__(self, fetch: Fetch, delay: float, sleep: Callable[[float], None]):
        self._fetch = fetch
        self._delay = delay
        self._sleep = sleep
        self.requests = 0

    def __call__(self, url: str) -> bytes:
        if self.requests:
            self._sleep(self._delay)
        self.requests += 1
        return self._fetch(url)


class RobotsRules:
    """One robots.txt per image host, read with the collector's user agent."""

    def __init__(self, fetch: Fetch):
        self._fetch = fetch
        self._parsers: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._parsers:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                body = self._fetch(origin + "/robots.txt")
                parser.parse(body.decode("utf-8", "replace").splitlines())
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    parser.disallow_all = True
                elif 400 <= exc.code < 500:
                    parser.allow_all = True  # no robots.txt: no rules
                else:
                    parser = None
            except (OSError, ValueError):
                parser = None  # unreadable rules: skip the host rather than guess
            self._parsers[origin] = parser
        parser = self._parsers[origin]
        return parser is not None and parser.can_fetch(USER_AGENT, url)


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


def _current_images(database: Database, source: str | None) -> dict[int, tuple[str, list[str]]]:
    return {
        row["id"]: (row["source_key"], json.loads(row["image_urls"]))
        for row in offer_rows(database, status="active")
        if row["source_key"] != "demo" and (source is None or row["source_key"] == source)
    }


def _kept_entries(path: Path, root: Path, offers: dict[int, tuple[str, list[str]]]) -> list[dict]:
    """Keep earlier entries whose file exists and whose URL is still on a current offer."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    kept = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            offer = offers.get(entry["offer_id"])
            exists = (root / entry["image_path"]).is_file()
        except (KeyError, TypeError):
            continue
        if offer and exists and entry["image_url"] in offer[1]:
            kept.append(entry)
    return kept


def fetch_images(
    database: Database,
    output_dir: Path,
    *,
    source: str | None = None,
    limit: int = 100,
    per_offer: int = 2,
    delay: float = 2.0,
    fetch: Fetch = http_get,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Download up to ``per_offer`` photos for each of ``limit`` stored offers."""
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
    # Earlier entries are pruned against all current offers, not only this source.
    every_offer = _current_images(database, None)
    entries = {
        (entry["offer_id"], entry["image_url"]): entry
        for entry in _kept_entries(manifest_path, root, every_offer)
    }
    throttle = Throttle(fetch, delay, sleep)
    robots = RobotsRules(throttle)
    counts: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    selected = sorted(_current_images(database, source).items())[:limit]
    for offer_id, (source_key, image_urls) in selected:
        for url in image_urls[:per_offer]:
            if (offer_id, url) in entries:
                counts["reused"] += 1
                continue
            if not retailer_image_url(source_key, url):
                failures["image URL is not on the retailer's HTTPS domains"] += 1
                continue
            stem = f"{offer_id}-{hashlib.sha256(url.encode()).hexdigest()[:16]}"
            folder = root / source_key
            existing = next(folder.glob(stem + ".*"), None) if folder.is_dir() else None
            if existing is None:
                if not robots.allowed(url):
                    failures["disallowed or unreadable robots.txt"] += 1
                    continue
                try:
                    data = throttle(url)
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
            entries[(offer_id, url)] = {
                "offer_id": offer_id,
                "image_url": url,
                "image_path": existing.relative_to(root).as_posix(),
            }
    manifest = sorted(entries.values(), key=lambda item: (item["offer_id"], item["image_url"]))
    if len(manifest) > MAX_ENTRIES:
        raise ValueError(f"manifest would exceed {MAX_ENTRIES} images; use a smaller limit")
    temporary = manifest_path.with_name(f".{MANIFEST_FILE}.tmp")
    temporary.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return {
        "status": "fetched" if not failures else "partial" if manifest else "failed",
        "offers_selected": len(selected),
        "downloaded": counts["downloaded"],
        "reused": counts["reused"],
        "requests": throttle.requests,
        "failures": dict(failures),
        "manifest_path": str(manifest_path),
        "manifest_entries": len(manifest),
        "retailer_rights_verified_by_tool": False,
    }

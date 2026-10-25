"""``fashion-finder index-photos``: build the hosted API's photo index from a runner.

The free Render API cannot encode its catalogue's photos (0.1 CPU, 512 MB, files
wiped on every wake-up), so the scheduled GitHub Actions run does it:

1. ``GET /api/v1/ingest/photos`` lists, per shop, the visible offers whose policy
   allows photo processing, their best photos and those already indexed;
2. offers are taken shops in turn up to ``--limit`` (offers already indexed count, so
   repeated runs keep the index at that size instead of growing it);
3. each photo not yet indexed gets its embedding from the **embedding cache** when
   possible (no request at all), otherwise it is downloaded with every check
   ``fetch-images`` makes (robots.txt and its Crawl-delay, TDMRep on the image host and
   the shop's site, opt-out headers, the retailer's own HTTPS domains, 5 MB and pixel
   limits), encoded, and its bytes dropped;
4. embeddings go to ``POST /api/v1/ingest/image-index`` in batches, at least every
   four minutes so the free instance never sleeps (and loses the catalogue) mid-run.

The embedding cache holds vectors only, keyed by photo URL, with the photo's SHA-256,
ETag, Last-Modified, the encoder version and the fetch time; never photo files. After
``--revalidate-days`` a cached photo is re-checked with a conditional request, and an
HTTP 304 (or identical bytes) keeps its vector.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .image_fetch import (
    Fetch,
    PhotoRefused,
    PolitePhotoFetcher,
    _interleave,
    checked_image,
    http_get,
    in_turn,
    retailer_image_url,
)
from .image_index import pack_features
from .runner import _batches
from .schema import utc_now
from .visual_encoder import get_encoder

CACHE_FILE = "embedding-cache.json"
CACHE_VERSION = 1
PAGE_SIZE = 500  # offers per listing request
UPLOAD_BATCH = 500  # the API's limit per upload
# Render's free plan sleeps after 15 idle minutes and its temporary catalogue goes with
# it; an upload or a health request at least this often keeps it awake.
KEEP_AWAKE_SECONDS = 240.0
MIN_PRUNE_DAYS = 60.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
COUNTERS = (
    "offers",
    "photos",
    "already_indexed",
    "from_cache",
    "downloaded",
    "revalidated_unchanged",
    "uploaded",
    "rejected",
    "skipped",
    "failed",
)
logger = logging.getLogger(__name__)


class PhotoIndexError(RuntimeError):
    """The run cannot start, e.g. the API indexes with another image model."""


def _valid_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        datetime.fromisoformat(record["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        isinstance(record.get("sha256"), str)
        and bool(_SHA256.match(record["sha256"]))
        and isinstance(record.get("features"), str)
        and isinstance(record.get("feature_version"), str)
        and all(isinstance(record.get(key), str | None) for key in ("etag", "last_modified"))
    )


class EmbeddingCache:
    """Photo embeddings kept between runs, keyed by photo URL; never photo bytes."""

    FIELDS = ("sha256", "etag", "last_modified", "feature_version", "fetched_at", "features")

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / CACHE_FILE
        self.records: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("starting a new embedding cache: %s is unreadable", self.path)
            return {}
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return {}
        photos = raw.get("photos")
        if not isinstance(photos, dict):
            return {}
        return {
            url: {field: record.get(field) for field in self.FIELDS}
            for url, record in photos.items()
            if isinstance(url, str) and _valid_record(record)
        }

    def usable(self, url: str, feature_version: str) -> dict[str, Any] | None:
        record = self.records.get(url)
        return record if record and record["feature_version"] == feature_version else None

    def same_photo(self, digest: str, feature_version: str) -> dict[str, Any] | None:
        """A record of identical bytes under another URL, encoded the same way."""
        return next(
            (
                record
                for record in self.records.values()
                if record["sha256"] == digest and record["feature_version"] == feature_version
            ),
            None,
        )

    def put(self, url: str, record: dict[str, Any]) -> None:
        self.records[url] = {field: record.get(field) for field in self.FIELDS}

    def prune(self, now: datetime, feature_version: str, keep_days: float) -> int:
        """Drop vectors of another encoder and photos not fetched for ``keep_days``.

        Photos still in the index are revalidated well within that, so their
        ``fetched_at`` stays recent.
        """
        cutoff = now - timedelta(days=keep_days)
        dropped = [
            url
            for url, record in self.records.items()
            if record["feature_version"] != feature_version
            or datetime.fromisoformat(record["fetched_at"]) < cutoff
        ]
        for url in dropped:
            del self.records[url]
        return len(dropped)

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{CACHE_FILE}.tmp")
        temporary.write_text(
            json.dumps(
                {"version": CACHE_VERSION, "photos": self.records},
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def _stale(record: dict[str, Any], now: datetime, revalidate_days: float) -> bool:
    age = now - datetime.fromisoformat(record["fetched_at"])
    return age.total_seconds() > revalidate_days * 86400


def run_index_photos(
    backend: Any,
    sources: list[str],
    *,
    cache_dir: Path,
    model_path: Path | None = None,
    limit: int = 1500,
    per_offer: int = 1,
    delay: float = 2.0,
    revalidate_days: float = 14.0,
    time_budget_minutes: float | None = None,
    fetch: Fetch = http_get,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Index up to ``limit`` offers' photos on the API; returns the run report.

    ``backend`` is a ``runner.ApiBackend`` (``photos``, ``push_photo_vectors`` and
    ``touch``). ``fetch``, ``sleep`` and ``clock`` are replaceable for tests.
    """
    encoder = get_encoder(model_path)
    started_at = utc_now()
    started = clock()
    run_id = f"photos-{started_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:6]}"
    budget = time_budget_minutes * 60 if time_budget_minutes else None
    cache = EmbeddingCache(cache_dir)
    fetcher = PolitePhotoFetcher(fetch, delay, sleep, clock)
    tally: dict[str, Counter] = {key: Counter({name: 0 for name in COUNTERS}) for key in sources}
    rejected: Counter = Counter()
    skipped: Counter = Counter()
    failures: Counter = Counter()

    listed: list[dict[str, Any]] = []
    for key in sources:
        offers: list[dict[str, Any]] = []
        after: int | None = 0
        while after is not None and len(offers) < limit:
            page = backend.photos(
                key, after=after, limit=min(PAGE_SIZE, limit - len(offers)), per_offer=per_offer
            )
            if page["feature_version"] != encoder.version:
                raise PhotoIndexError(
                    f"the API indexes with {page['feature_version']}, but this run would"
                    f" encode with {encoder.version}; pass the same --model-path"
                )
            offers.extend(page["offers"])
            after = page["next_after"]
        listed.extend(offers[:limit])
    last_contact = clock()

    from_cache: list[tuple[dict, str, dict]] = []
    to_fetch: list[tuple[dict, str, dict | None]] = []
    now = utc_now()
    for offer in in_turn(listed, lambda offer: offer["source_key"])[:limit]:
        counts = tally[offer["source_key"]]
        counts["offers"] += 1
        for url in offer["image_urls"]:
            counts["photos"] += 1
            if url in offer["indexed_image_urls"]:
                counts["already_indexed"] += 1
                continue
            record = cache.usable(url, encoder.version)
            if record is not None and not _stale(record, now, revalidate_days):
                from_cache.append((offer, url, record))
            else:
                to_fetch.append((offer, url, record))

    pending: list[dict[str, Any]] = []

    def flush(force: bool = False) -> None:
        nonlocal last_contact
        if (
            not force
            and len(pending) < UPLOAD_BATCH
            and clock() - last_contact < KEEP_AWAKE_SECONDS
        ):
            return
        if pending:
            for batch in _batches(pending):
                results = backend.push_photo_vectors(encoder.version, batch, run_id=run_id)
                for result in results:
                    counts = tally[batch[result["index"]]["source_key"]]
                    if result["status"] == "indexed":
                        counts["uploaded"] += 1
                    else:
                        counts["rejected"] += 1
                        rejected[result.get("reason") or "rejected"] += 1
            pending.clear()
            cache.save()
        elif not force:
            backend.touch()
        last_contact = clock()

    def queue(offer: dict[str, Any], url: str, record: dict[str, Any]) -> None:
        pending.append(
            {
                "source_key": offer["source_key"],
                "offer_url": offer["offer_url"],
                "image_url": url,
                "sha256": record["sha256"],
                "features": record["features"],
            }
        )

    def index_one(offer: dict[str, Any], url: str, known: dict[str, Any] | None) -> None:
        counts = tally[offer["source_key"]]
        refusal = (
            None
            if retailer_image_url(offer["source_key"], url)
            else "image URL is not on the retailer's HTTPS domains"
        ) or fetcher.refusal(offer["source_key"], url)
        if refusal:
            counts["skipped"] += 1
            skipped[refusal] += 1
            return
        try:
            fetched = fetcher.download(url, known)
        except PhotoRefused as exc:
            counts["skipped" if exc.opted_out else "failed"] += 1
            (skipped if exc.opted_out else failures)[str(exc)] += 1
            return
        fetched_at = utc_now().isoformat()
        if fetched is None:  # 304 Not Modified: the cached vector still describes it
            record = {**known, "fetched_at": fetched_at}
            counts["revalidated_unchanged"] += 1
        else:
            data, headers = fetched
            digest = hashlib.sha256(data).hexdigest()
            validators = {
                "etag": headers.get("etag"),
                "last_modified": headers.get("last-modified"),
            }
            if known is not None and known["sha256"] == digest:
                record = {**known, **validators, "fetched_at": fetched_at}
                counts["revalidated_unchanged"] += 1
            else:
                twin = cache.same_photo(digest, encoder.version)
                if twin is not None:
                    features = twin["features"]
                else:
                    try:
                        with checked_image(data) as image:
                            features = pack_features(encoder.encode(image))
                    except ValueError as exc:
                        counts["failed"] += 1
                        failures[str(exc)] += 1
                        return
                record = {
                    "sha256": digest,
                    **validators,
                    "feature_version": encoder.version,
                    "fetched_at": fetched_at,
                    "features": features,
                }
                counts["downloaded"] += 1
            del data, fetched  # the photo itself is never kept, in the cache or elsewhere
        cache.put(url, record)
        queue(offer, url, record)

    stopped = None
    not_attempted = 0
    try:
        for offer, url, record in from_cache:
            tally[offer["source_key"]]["from_cache"] += 1
            queue(offer, url, record)
            flush()
        work = _interleave(to_fetch)
        for position, (offer, url, known) in enumerate(work):
            if budget is not None and clock() - started + fetcher.wait_for(url) >= budget:
                stopped = f"time budget of {time_budget_minutes:g} minutes reached"
                not_attempted = len(work) - position
                break
            index_one(offer, url, known)
            flush()
        flush(force=True)
    finally:
        cache.prune(utc_now(), encoder.version, max(MIN_PRUNE_DAYS, 2 * revalidate_days))
        cache.save()
    finished_at = utc_now()
    totals = sum(tally.values(), Counter())
    return {
        "run_id": run_id,
        "backend": "api",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
        "feature_version": encoder.version,
        **{name: totals[name] for name in COUNTERS},
        "rejected_reasons": dict(rejected),
        "skipped_reasons": dict(skipped),
        "failure_reasons": dict(failures),
        "image_requests": fetcher.photo_requests,
        "requests": fetcher.requests,
        "crawl_delays": fetcher.crawl_delays,
        "stopped": stopped,
        "not_attempted": not_attempted,
        "cache_entries": len(cache.records),
        "sources": {key: dict(counts) for key, counts in tally.items() if counts["offers"]},
    }


def photo_markdown(report: dict[str, Any], title: str) -> str:
    """The run report as Markdown for ``$GITHUB_STEP_SUMMARY``."""
    version = report["feature_version"].split(":")[0]
    lines = [
        f"## {title}",
        "",
        (
            f"Run `{report['run_id']}`, {report['started_at'][:16]}Z,"
            f" {report['duration_seconds']} s; encoder `{version}`."
        ),
        "",
        (
            "| Source | Offers | Already indexed | From cache | Downloaded | Revalidated"
            " | Uploaded | Rejected | Skipped (robots/TDM/opt-out) | Failed |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, row in report["sources"].items():
        lines.append(
            f"| {key} | {row['offers']} | {row['already_indexed']} | {row['from_cache']} |"
            f" {row['downloaded']} | {row['revalidated_unchanged']} | {row['uploaded']} |"
            f" {row['rejected']} | {row['skipped']} | {row['failed']} |"
        )
    if not report["sources"]:
        lines.append("| - | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |")
    lines += [
        "",
        (
            f"Photo requests: {report['image_requests']} ({report['requests']} with"
            f" robots.txt and TDMRep). Embedding cache: {report['cache_entries']} photos."
        ),
    ]
    if report["crawl_delays"]:
        delays = ", ".join(f"{host} {delay:g} s" for host, delay in report["crawl_delays"].items())
        lines.append(f"Crawl-delay honoured: {delays}.")
    for label, key in (
        ("Rejected by the API", "rejected_reasons"),
        ("Skipped", "skipped_reasons"),
        ("Failed", "failure_reasons"),
    ):
        if report[key]:
            reasons = ", ".join(f"{reason}: {count}" for reason, count in report[key].items())
            lines.append(f"\n- **{label}:** {reasons}")
    if report["stopped"]:
        lines.append(
            f"\n- **Stopped:** {report['stopped']}; {report['not_attempted']} photos are left"
            " for the next run."
        )
    return "\n".join(lines) + "\n"

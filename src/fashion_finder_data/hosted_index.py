"""The photo index of a server that cannot encode its catalogue's photos itself.

The free Render API has 0.1 CPU, 512 MB and loses its files on every wake-up, so a
runner (``fashion-finder index-photos`` on GitHub Actions) downloads and encodes the
photos and uploads only their embeddings. This module is the server's side:

- :func:`photo_listing` pages through visible offers whose policy allows photo
  processing, with the photos to encode and those already indexed;
- :func:`apply_image_upload` checks each uploaded embedding against the catalogue,
  the source policy and the API's own encoder, then merges the accepted ones into
  ``product-image-index.json`` (format version 2, as ``build_image_index`` writes).

Offers are named by source key and URL, never by database id: ids change when the
temporary catalogue is rebuilt, URLs do not.
"""

from __future__ import annotations

import binascii
import json
import math
import re
from datetime import UTC, datetime
from typing import Any

from django.apps import apps
from django.db.models import F, Q
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .image_index import (
    MAX_ENTRIES,
    image_skip_reason,
    merge_index_entries,
    stored_index_entries,
    unpack_features,
)
from .image_selection import select_photos
from .public_catalogue import _safe_image_url
from .schema import canonical_url
from .source_policy import SourcePolicy
from .sources import NON_RETAIL_SOURCES, SOURCES
from .storage import Database
from .visual_encoder import ImageEncoder

MAX_UPLOAD_ENTRIES = 500
# float32 storage of a unit vector is accurate to about 1e-7; anything further off was
# not produced by the model's L2 normalisation.
NORM_TOLERANCE = 1e-3
LOOKUP_CHUNK = 200
SCAN_CAP = 10_000  # offer rows one listing request reads at most
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LISTING_FIELDS = (
    "id",
    "canonical_url",
    "source_url",
    "status",
    "availability_status",
    "currency",
    "price_amount",
    "source_fetched_at",
    "colour_names",
    "size_options",
    "image_urls",
)


class UploadEntry(BaseModel):
    """One photo embedding encoded by a runner."""

    model_config = ConfigDict(extra="forbid")

    source_key: str = Field(min_length=1, max_length=40)
    offer_url: str = Field(min_length=1, max_length=2048)
    image_url: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=_SHA256.pattern)
    features: str = Field(min_length=1, max_length=65_536)


class ImageIndexUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    feature_version: str = Field(min_length=1, max_length=300)
    # Validated one by one, so one bad entry is rejected on its own.
    entries: list[Any] = Field(max_length=MAX_UPLOAD_ENTRIES)


def _offers():
    return apps.get_model("catalogue", "RetailerOffer")


def _rows(database: Database):
    return (
        _offers()
        .objects.using(database.alias)
        .annotate(source_key=F("retailer__source_key"), market_code=F("retailer__market_code"))
    )


def _offer_lookup(
    database: Database, wanted: set[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """Offers by (source key, URL), matched on the canonical URL first, then the page URL.

    Batched: one query per source and ``LOOKUP_CHUNK`` URLs.
    """
    by_source: dict[str, set[str]] = {}
    for source_key, url in wanted:
        by_source.setdefault(source_key, set()).add(url)
    canonical: dict[tuple[str, str], dict[str, Any]] = {}
    page: dict[tuple[str, str], dict[str, Any]] = {}
    for source_key, urls in by_source.items():
        ordered = sorted(urls)
        for start in range(0, len(ordered), LOOKUP_CHUNK):
            chunk = ordered[start : start + LOOKUP_CHUNK]
            rows = (
                _rows(database)
                .filter(retailer__source_key=source_key, retailer__market_code="HU")
                .filter(Q(canonical_url__in=chunk) | Q(source_url__in=chunk))
                .values(*_LISTING_FIELDS, "source_key", "market_code")
            )
            for row in rows:
                canonical[(source_key, row["canonical_url"])] = row
                page[(source_key, row["source_url"])] = row
    return {
        key: canonical.get(key) or page[key] for key in wanted if key in canonical or key in page
    }


def _url_forms(url: str) -> list[str]:
    try:
        cleaned = canonical_url(url)
    except ValueError:
        return [url]
    return [url] if cleaned == url else [url, cleaned]


def vector_problem(text: str, encoder: ImageEncoder) -> str | None:
    """Why an uploaded embedding cannot sit beside the encoder's own, or None."""
    try:
        values = unpack_features(text)
    except (ValueError, binascii.Error):
        return "features must be base64 of little-endian float32 values"
    if len(values) != encoder.length:
        return f"vector has {len(values)} values; this encoder makes {encoder.length}"
    if not all(math.isfinite(value) for value in values):
        return "vector has non-finite values"
    if encoder.cosine:
        norm = math.sqrt(math.fsum(value * value for value in values))
        if abs(norm - 1.0) > NORM_TOLERANCE:
            return f"vector is not L2-normalised (length {norm:.4f})"
    return None


def _reason(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(part) for part in first.get("loc", ())) or "entry"
    return f"{where}: {first.get('msg', 'invalid')}"[:160]


def apply_image_upload(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    encoder: ImageEncoder,
    entries: list[Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check each uploaded embedding and merge the accepted ones into the index.

    The caller has already checked that the upload's feature version is
    ``encoder.version``. Returns per-entry results (``indexed`` with the offer id, or
    ``rejected`` with a reason) and the number of photos now indexed.
    """
    if len(entries) > MAX_UPLOAD_ENTRIES:
        raise ValueError(f"an upload holds at most {MAX_UPLOAD_ENTRIES} entries")
    now = now or datetime.now(UTC)
    results: list[dict[str, Any] | None] = [None] * len(entries)
    parsed: dict[int, UploadEntry] = {}

    def reject(index: int, reason: str, source_key: str | None = None) -> None:
        results[index] = {
            "index": index,
            "source_key": source_key if source_key in SOURCES else None,
            "status": "rejected",
            "reason": reason,
        }

    for index, raw in enumerate(entries):
        try:
            entry = UploadEntry.model_validate(raw)
        except ValidationError as exc:
            source_key = raw.get("source_key") if isinstance(raw, dict) else None
            reject(index, _reason(exc), source_key if isinstance(source_key, str) else None)
            continue
        if entry.source_key in NON_RETAIL_SOURCES:
            # Demo and practice rows live in their own data dirs, never a real catalogue.
            reject(index, f"{entry.source_key} is not a retailer source")
        elif entry.source_key not in SOURCES:
            reject(index, "unknown source")
        else:
            parsed[index] = entry
    wanted = {
        (entry.source_key, form)
        for entry in parsed.values()
        for form in _url_forms(entry.offer_url)
    }
    offers = _offer_lookup(database, wanted)
    accepted: list[dict[str, Any]] = []
    positions: list[int] = []
    seen: set[tuple[int, str]] = set()
    for index, entry in parsed.items():
        offer = next(
            (
                offers[(entry.source_key, form)]
                for form in _url_forms(entry.offer_url)
                if (entry.source_key, form) in offers
            ),
            None,
        )
        if offer is None:
            reject(index, "unknown offer for this source", entry.source_key)
            continue
        reason = image_skip_reason(offer, entry.image_url, policies, now)
        if reason is None and not _safe_image_url(entry.image_url):
            reason = "image URL must use HTTPS without credentials"
        reason = reason or vector_problem(entry.features, encoder)
        if reason is None and (offer["id"], entry.image_url) in seen:
            reason = "duplicate photo in this upload"
        if reason:
            reject(index, reason, entry.source_key)
            continue
        seen.add((offer["id"], entry.image_url))
        policy = policies[(offer["source_key"], offer["market_code"])]
        accepted.append(
            {
                "offer_id": offer["id"],
                "source_key": offer["source_key"],
                "image_url": entry.image_url,
                "policy_version": policy.policy_version,
                "sha256": entry.sha256,
                "features": entry.features,
            }
        )
        positions.append(index)
    total, refused = merge_index_entries(database, encoder.version, accepted)
    refused_positions = {positions[position] for position in refused}
    for position, index in enumerate(positions):
        if index in refused_positions:
            reject(
                index, f"the photo index is full ({MAX_ENTRIES} photos)", parsed[index].source_key
            )
        else:
            results[index] = {
                "index": index,
                "source_key": parsed[index].source_key,
                "status": "indexed",
                "offer_id": accepted[position]["offer_id"],
            }
    return {"results": results, "index_entries": total}


def photo_listing(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    feature_version: str,
    source_key: str,
    *,
    after_id: int = 0,
    limit: int = 500,
    per_offer: int = 1,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], int | None]:
    """A page of visible offers whose photos may be processed, in id order.

    Each offer lists its best ``per_offer`` photo URLs (``select_photos``) and those
    already in the index with this feature version and the current policy version, so
    a runner encodes only the rest. Returns the offers and the cursor for the next page
    (None at the end). Rows are read in batches; one request reads at most
    ``SCAN_CAP`` rows.
    """
    now = now or datetime.now(UTC)
    policy = policies.get((source_key, "HU"))
    if policy is None or not policy.allow_image_processing:
        return [], None
    indexed = {
        (entry["offer_id"], entry["image_url"])
        for entry in stored_index_entries(database, feature_version)
        if entry["policy_version"] == policy.policy_version
    }
    rows = (
        _rows(database)
        .filter(
            retailer__source_key=source_key,
            retailer__market_code="HU",
            status="active",
            id__gt=after_id,
        )
        .order_by("id")
        .values(*_LISTING_FIELDS, "source_key", "market_code")
    )
    offers: list[dict[str, Any]] = []
    last_id = after_id
    scanned = 0
    for row in rows[:SCAN_CAP].iterator(chunk_size=500):
        scanned += 1
        last_id = row["id"]
        try:
            stored = json.loads(row["image_urls"] or "[]")
        except ValueError:
            continue
        urls = [
            url
            for url in select_photos(stored if isinstance(stored, list) else [], per_offer)
            if image_skip_reason(row, url, policies, now) is None
        ]
        if not urls:
            continue
        offers.append(
            {
                "source_key": source_key,
                "offer_url": row["canonical_url"],
                "image_urls": urls,
                "indexed_image_urls": [url for url in urls if (row["id"], url) in indexed],
            }
        )
        if len(offers) >= limit:
            break
    more = len(offers) >= limit or scanned >= SCAN_CAP
    return offers, last_id if more else None

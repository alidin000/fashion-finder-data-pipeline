"""Offline index of explicitly permitted local product photos."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import sys
from array import array
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from .public_catalogue import _public_offer, _safe_image_url, _visible
from .source_policy import SourcePolicy
from .storage import Database, offer_rows
from .text_match import matches_colour, matches_size
from .visual_encoder import ImageEncoder, get_encoder, get_text_encoder

INDEX_FILE = "product-image-index.json"
# Version 2 stores each embedding as base64 little-endian float32, not a JSON list.
INDEX_VERSION = 2
MAX_ENTRIES = 20_000
RESULT_LIMIT = 5
CONSENSUS_NEIGHBOURS = 20
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


class ImageManifestEntry(BaseModel):
    """One local file already supplied under an approved source policy."""

    model_config = ConfigDict(extra="forbid")

    offer_id: int = Field(gt=0)
    image_url: str = Field(min_length=1, max_length=2048)
    image_path: str = Field(min_length=1, max_length=500)


def pack_features(values: list[float]) -> str:
    data = array("f", values)
    if sys.byteorder == "big":
        data.byteswap()
    return base64.b64encode(data.tobytes()).decode("ascii")


def unpack_features(text: str) -> array:
    data = array("f")
    data.frombytes(base64.b64decode(text, validate=True))
    if sys.byteorder == "big":
        data.byteswap()
    return data


def _read_image(path: Path, encoder: ImageEncoder) -> tuple[bytes, list[float]]:
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds 5 MB: {path.name}")
    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds 5 MB: {path.name}")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError(f"unsupported image encoding: {path.name}")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError(f"image dimensions are too large: {path.name}")
            image.load()
            return data, encoder.encode(image)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"cannot decode image: {path.name}") from exc


def build_image_index(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    manifest_path: Path,
    *,
    model_path: Path | None = None,
) -> dict[str, Any]:
    """Validate every entry, then atomically replace the local descriptor index."""
    encoder = get_encoder(model_path)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ENTRIES:
        raise ValueError(f"manifest must contain 1–{MAX_ENTRIES} image entries")
    root = manifest_path.resolve().parent
    indexed = []
    seen = set()
    for number, item in enumerate(raw, start=1):
        entry = ImageManifestEntry.model_validate(item)
        key = (entry.offer_id, entry.image_url)
        if key in seen:
            raise ValueError(f"duplicate manifest image at entry {number}")
        seen.add(key)
        if not _safe_image_url(entry.image_url):
            raise ValueError(f"entry {number}: image URL must use HTTPS without credentials")
        offer = next(iter(offer_rows(database, id=entry.offer_id)), None)
        if offer is None or offer["source_key"] == "demo":
            raise ValueError(f"entry {number}: no eligible retailer offer")
        policy = policies.get((offer["source_key"], offer["market_code"]))
        if (
            policy is None
            or not policy.allow_image_display
            or not policy.allow_image_processing
            or _visible(offer, policies, datetime.now(UTC)) is None
            or entry.image_url not in json.loads(offer["image_urls"])
        ):
            raise ValueError(f"entry {number}: image is not permitted on a current offer")
        relative = Path(entry.image_path)
        if relative.is_absolute():
            raise ValueError(f"entry {number}: image path must be relative to the manifest")
        image_path = (root / relative).resolve()
        if not image_path.is_relative_to(root) or not image_path.is_file():
            raise ValueError(f"entry {number}: image path is outside the manifest directory")
        data, features = _read_image(image_path, encoder)
        indexed.append(
            {
                "offer_id": entry.offer_id,
                "source_key": offer["source_key"],
                "image_url": entry.image_url,
                "policy_version": policy.policy_version,
                "sha256": hashlib.sha256(data).hexdigest(),
                "features": pack_features(features),
            }
        )
    destination = database.path.parent / INDEX_FILE
    temporary = destination.with_name(f".{INDEX_FILE}.tmp")
    temporary.write_text(
        json.dumps(
            {"version": INDEX_VERSION, "feature_version": encoder.version, "entries": indexed},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(destination)
    return {
        "status": "indexed",
        "indexed_images": len(indexed),
        "feature_version": encoder.version,
        "index_path": str(destination),
        "retailer_rights_verified_by_tool": False,
    }


@lru_cache(maxsize=2)
def _read_index(
    path: str, inode: int, size: int, modified_ns: int, feature_version: str, length: int
) -> tuple[dict[str, Any], ...]:
    """Parse and validate once per index file; a rebuild changes the cache key."""
    index = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = index["entries"]
    if (
        index["version"] != INDEX_VERSION
        or index["feature_version"] != feature_version
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= MAX_ENTRIES
    ):
        raise ValueError("image index is incompatible")
    for entry in entries:
        if (
            not isinstance(entry["offer_id"], int)
            or not isinstance(entry["image_url"], str)
            or not isinstance(entry["source_key"], str)
            or not isinstance(entry["policy_version"], str)
            or not isinstance(entry["features"], str)
        ):
            raise TypeError("image index contains invalid entries")
        values = unpack_features(entry["features"])
        if len(values) != length or not all(math.isfinite(value) for value in values):
            raise ValueError("image index contains invalid entries")
        entry["features"] = values
    return tuple(entries)


def _load_index(database: Database, encoder: ImageEncoder) -> tuple[dict[str, Any], ...]:
    path = database.path.parent / INDEX_FILE
    try:
        stat = path.stat()
        return _read_index(
            str(path),
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            encoder.version,
            encoder.length,
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, binascii.Error) as exc:
        raise ValueError("image index is unavailable or invalid") from exc


def _distances(
    encoder: ImageEncoder, query: list[float], entries: list[dict[str, Any]]
) -> list[float]:
    """Cosine models rank with one matrix product when NumPy is installed."""
    if encoder.cosine and entries:
        try:
            import numpy as np
        except ImportError:
            pass
        else:
            matrix = np.frombuffer(
                b"".join(entry["features"].tobytes() for entry in entries), dtype=np.float32
            ).reshape(len(entries), encoder.length)
            return (1.0 - matrix @ np.asarray(query, dtype=np.float32)).tolist()
    return [encoder.distance(query, entry["features"]) for entry in entries]


def _eligible(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    entries: list[dict[str, Any]],
    *,
    source: str | None = None,
    category: str | None = None,
    colour: str | None = None,
    size: str | None = None,
    max_price: Decimal | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if max_price is not None and (not max_price.is_finite() or max_price < 0):
        raise ValueError("max price must be finite and nonnegative")
    rows = {row["id"]: row for row in offer_rows(database, id__in=[e["offer_id"] for e in entries])}
    current = datetime.now(UTC)
    matches = []
    for entry in entries:
        row = rows.get(entry["offer_id"])
        if row is None or row["source_key"] == "demo" or row["source_key"] != entry["source_key"]:
            continue
        policy = _visible(row, policies, current)
        if (
            policy is None
            or not policy.allow_image_display
            or not policy.allow_image_processing
            or policy.policy_version != entry["policy_version"]
            or entry["image_url"] not in json.loads(row["image_urls"])
        ):
            continue
        offer = _public_offer(row, policy)
        if entry["image_url"] not in offer["image_urls"]:
            continue
        if source and offer["source_key"] != source:
            continue
        if category and offer["category"] != category:
            continue
        if colour and not matches_colour(colour, offer["colour_names"]):
            continue
        if size and not matches_size(size, offer["size_options"]):
            continue
        if max_price is not None and Decimal(offer["price_amount"]) > max_price:
            continue
        matches.append((entry, offer))
    return matches


def image_index_ready(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    *,
    model_path: Path | None = None,
) -> bool:
    try:
        return bool(_eligible(database, policies, _load_index(database, get_encoder(model_path))))
    except ValueError:
        return False


def consensus_category(offers: list[dict[str, Any]]) -> str | None:
    """Return the category holding a rank-weighted majority of the closest offers.

    Weights fall as 1, 1/2, 1/3, … so the nearest matches count most. Without a
    clear majority the visual order is kept unchanged.
    """
    votes: Counter[str] = Counter()
    total = 0.0
    for position, offer in enumerate(offers[:CONSENSUS_NEIGHBOURS]):
        weight = 1 / (position + 1)
        total += weight
        if offer["category"]:
            votes[offer["category"]] += weight
    if not votes:
        return None
    category, weight = votes.most_common(1)[0]
    return category if weight > total / 2 else None


def _rank(
    encoder: ImageEncoder,
    query: list[float],
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    consensus: bool,
) -> dict[str, Any]:
    distances = _distances(encoder, query, [entry for entry, _ in candidates])
    order = sorted(range(len(candidates)), key=lambda i: (distances[i], candidates[i][1]["id"]))
    offers: list[dict[str, Any]] = []
    seen: set[int] = set()
    for position in order:
        offer = candidates[position][1]
        if offer["id"] not in seen:  # an offer's best photo decides its place
            seen.add(offer["id"])
            offers.append(offer)
    result: dict[str, Any] = {"items": [], "next_cursor": None}
    preferred = consensus_category(offers) if consensus else None
    if preferred:
        # A red-dress photo should list dresses before red bags; the sort is stable.
        offers.sort(key=lambda offer: offer["category"] != preferred)
        result["preferred_category"] = preferred
    result["items"] = offers[:RESULT_LIMIT]
    return result


def search_indexed_image(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    image: Image.Image,
    *,
    source: str | None = None,
    category: str | None = None,
    colour: str | None = None,
    size: str | None = None,
    max_price: Decimal | None = None,
    model_path: Path | None = None,
    consensus: bool = True,
) -> dict[str, Any]:
    encoder = get_encoder(model_path)
    query = encoder.encode(image)
    candidates = _eligible(
        database,
        policies,
        _load_index(database, encoder),
        source=source,
        category=category,
        colour=colour,
        size=size,
        max_price=max_price,
    )
    return _rank(encoder, query, candidates, consensus=consensus)


def _text_encoder_for(
    image_encoder: ImageEncoder, text_model_path: Path | None, tokenizer_path: Path | None
):
    text_encoder = get_text_encoder(text_model_path, tokenizer_path)
    if text_encoder.space != image_encoder.space:
        raise ValueError("text-to-photo search needs an index built with Marqo-FashionSigLIP")
    return text_encoder


def text_index_ready(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    *,
    model_path: Path | None = None,
    text_model_path: Path | None = None,
    tokenizer_path: Path | None = None,
) -> bool:
    try:
        _text_encoder_for(get_encoder(model_path), text_model_path, tokenizer_path)
    except ValueError:
        return False
    return image_index_ready(database, policies, model_path=model_path)


def search_indexed_text(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    text: str,
    *,
    source: str | None = None,
    category: str | None = None,
    colour: str | None = None,
    size: str | None = None,
    max_price: Decimal | None = None,
    model_path: Path | None = None,
    text_model_path: Path | None = None,
    tokenizer_path: Path | None = None,
    consensus: bool = True,
) -> dict[str, Any]:
    """Rank indexed product photos by how well they match a description."""
    if not text.strip():
        raise ValueError("describe the look to search for")
    image_encoder = get_encoder(model_path)
    text_encoder = _text_encoder_for(image_encoder, text_model_path, tokenizer_path)
    candidates = _eligible(
        database,
        policies,
        _load_index(database, image_encoder),
        source=source,
        category=category,
        colour=colour,
        size=size,
        max_price=max_price,
    )
    return _rank(image_encoder, text_encoder.encode(text), candidates, consensus=consensus)

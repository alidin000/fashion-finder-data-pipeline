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
from .text_match import (
    english_query,
    matches_colour,
    matches_size,
    matches_terms,
    query_terms,
    relevance,
    row_text,
)
from .visual_encoder import ImageEncoder, get_encoder, get_text_encoder

INDEX_FILE = "product-image-index.json"
# Version 2 stores each embedding as base64 little-endian float32, not a JSON list.
INDEX_VERSION = 2
MAX_ENTRIES = 20_000
RESULT_LIMIT = 5
LOOK_LIMIT = 20
CONSENSUS_NEIGHBOURS = 20
RRF_K = 60  # reciprocal rank fusion constant; Elasticsearch's default
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


def image_skip_reason(
    offer: dict[str, Any] | None,
    image_url: str,
    policies: dict[tuple[str, str], SourcePolicy],
    now: datetime,
) -> str | None:
    """Why a photo cannot be indexed now, or None; shared by fetching and indexing."""
    if offer is None or offer["source_key"] == "demo":
        return "no retailer offer"
    policy = policies.get((offer["source_key"], offer["market_code"]))
    if policy is None or not policy.allow_image_display or not policy.allow_image_processing:
        return "source policy does not permit images"
    if _visible(offer, policies, now) is None:
        return "offer is not currently visible (stale, out of stock or unapproved)"
    if image_url not in json.loads(offer["image_urls"]):
        return "photo URL is no longer on the offer"
    return None


def build_image_index(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    manifest_path: Path,
    *,
    model_path: Path | None = None,
) -> dict[str, Any]:
    """Index every eligible manifest photo, then atomically replace the index.

    A malformed or unsafe manifest stops the build. A photo whose offer is no
    longer eligible is skipped and counted, so one sold-out item cannot block it.
    """
    encoder = get_encoder(model_path)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ENTRIES:
        raise ValueError(f"manifest must contain 1–{MAX_ENTRIES} image entries")
    root = manifest_path.resolve().parent
    entries = [ImageManifestEntry.model_validate(item) for item in raw]
    offers = {
        row["id"]: row
        for row in offer_rows(database, id__in=sorted({entry.offer_id for entry in entries}))
    }
    now = datetime.now(UTC)
    indexed = []
    seen = set()
    skipped: Counter[str] = Counter()
    for number, entry in enumerate(entries, start=1):
        key = (entry.offer_id, entry.image_url)
        if key in seen:
            raise ValueError(f"duplicate manifest image at entry {number}")
        seen.add(key)
        if not _safe_image_url(entry.image_url):
            raise ValueError(f"entry {number}: image URL must use HTTPS without credentials")
        relative = Path(entry.image_path)
        if relative.is_absolute():
            raise ValueError(f"entry {number}: image path must be relative to the manifest")
        image_path = (root / relative).resolve()
        if not image_path.is_relative_to(root) or not image_path.is_file():
            raise ValueError(f"entry {number}: image path is outside the manifest directory")
        offer = offers.get(entry.offer_id)
        reason = image_skip_reason(offer, entry.image_url, policies, now)
        if reason:
            skipped[reason] += 1
            continue
        policy = policies[(offer["source_key"], offer["market_code"])]
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
    if not indexed:
        raise ValueError(f"no eligible images to index; skipped: {dict(skipped)}")
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
        "status": "indexed" if not skipped else "partial",
        "indexed_images": len(indexed),
        "skipped_images": dict(skipped),
        "feature_version": encoder.version,
        "index_path": str(destination),
        "retailer_rights_verified_by_tool": False,
    }


def _stack(entries: list[dict[str, Any]] | tuple[dict[str, Any], ...], length: int):
    """One float32 matrix of the entries' embeddings, or None without NumPy."""
    try:
        import numpy as np
    except ImportError:
        return None
    return np.frombuffer(
        b"".join(entry["features"].tobytes() for entry in entries), dtype=np.float32
    ).reshape(len(entries), length)


@lru_cache(maxsize=2)
def _read_index(
    path: str, inode: int, size: int, modified_ns: int, feature_version: str, length: int
) -> tuple[tuple[dict[str, Any], ...], Any]:
    """Parse, validate and stack once per index file; a rebuild changes the cache key."""
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
    for row, entry in enumerate(entries):
        entry["_row"] = row  # position in the cached matrix
    return tuple(entries), _stack(entries, length)


def _load_index(
    database: Database, encoder: ImageEncoder
) -> tuple[tuple[dict[str, Any], ...], Any]:
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
    encoder: ImageEncoder, query: list[float], entries: list[dict[str, Any]], matrix=None
) -> list[float]:
    """Cosine models rank with one matrix product when NumPy is installed.

    With the cached index matrix, one product covers every photo and the candidates
    pick their rows; otherwise the candidates are stacked for this query.
    """
    if encoder.cosine and entries:
        try:
            import numpy as np
        except ImportError:
            pass
        else:
            vector = np.asarray(query, dtype=np.float32)
            if matrix is not None and all("_row" in entry for entry in entries):
                every = 1.0 - matrix @ vector
                return [float(every[entry["_row"]]) for entry in entries]
            return (1.0 - _stack(entries, encoder.length) @ vector).tolist()
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
    """Cheap check for health probes: a compatible index and one permitted source.

    Per-offer freshness and policy checks still run on every search.
    """
    try:
        entries, _ = _load_index(database, get_encoder(model_path))
    except ValueError:
        return False
    permitted = {
        key
        for (key, _market), policy in policies.items()
        if policy.approval_status == "approved"
        and policy.allow_image_display
        and policy.allow_image_processing
    }
    return any(entry["source_key"] in permitted for entry in entries)


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


def _visual_order(
    encoder: ImageEncoder,
    query: list[float],
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
    matrix=None,
) -> list[dict[str, Any]]:
    """Distinct offers, nearest first; an offer's best photo decides its place."""
    distances = _distances(encoder, query, [entry for entry, _ in candidates], matrix)
    order = sorted(range(len(candidates)), key=lambda i: (distances[i], candidates[i][1]["id"]))
    offers: list[dict[str, Any]] = []
    seen: set[int] = set()
    for position in order:
        offer = candidates[position][1]
        if offer["id"] not in seen:
            seen.add(offer["id"])
            offers.append(offer)
    return offers


def _rank(
    encoder: ImageEncoder,
    query: list[float],
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    consensus: bool,
    matrix=None,
    limit: int = RESULT_LIMIT,
) -> dict[str, Any]:
    offers = _visual_order(encoder, query, candidates, matrix)
    result: dict[str, Any] = {"items": [], "next_cursor": None}
    preferred = consensus_category(offers) if consensus else None
    if preferred and offers:
        # A red-dress photo should list dresses before red bags, but the single
        # nearest match always stays first; the sort is stable.
        rest = sorted(offers[1:], key=lambda offer: offer["category"] != preferred)
        offers = [offers[0], *rest]
        result["preferred_category"] = preferred
    result["items"] = offers[:limit]
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
    entries, matrix = _load_index(database, encoder)
    candidates = _eligible(
        database,
        policies,
        entries,
        source=source,
        category=category,
        colour=colour,
        size=size,
        max_price=max_price,
    )
    return _rank(encoder, query, candidates, consensus=consensus, matrix=matrix)


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
    limit: int = LOOK_LIMIT,
    blend: bool = True,
) -> dict[str, Any]:
    """Rank indexed product photos by how well they match a description.

    Hungarian clothing and colour words are translated first, because the model's
    text tower only understands English. With ``blend``, the visual order is fused
    with word relevance by reciprocal rank fusion, so an offer whose title names
    the item ranks above one that merely looks similar.
    """
    if not text.strip():
        raise ValueError("describe the look to search for")
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    image_encoder = get_encoder(model_path)
    text_encoder = _text_encoder_for(image_encoder, text_model_path, tokenizer_path)
    entries, matrix = _load_index(database, image_encoder)
    candidates = _eligible(
        database,
        policies,
        entries,
        source=source,
        category=category,
        colour=colour,
        size=size,
        max_price=max_price,
    )
    visual = _visual_order(
        image_encoder, text_encoder.encode(english_query(text)), candidates, matrix
    )
    if not blend:
        return {"items": visual[:limit], "next_cursor": None}
    terms = query_terms(text)
    lexical = sorted(
        (offer for offer in visual if matches_terms(terms, row_text(offer))),
        key=lambda offer: -relevance(terms, offer),
    )
    scores: dict[int, float] = {}
    for ranking in (visual, lexical):
        for position, offer in enumerate(ranking, start=1):
            scores[offer["id"]] = scores.get(offer["id"], 0.0) + 1 / (RRF_K + position)
    fused = sorted(visual, key=lambda offer: -scores[offer["id"]])  # stable: ties stay visual
    return {"items": fused[:limit], "next_cursor": None}

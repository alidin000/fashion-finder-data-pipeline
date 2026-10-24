"""Offline index of explicitly permitted local product photos."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import math
import re
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

from . import look_search
from .look_query import TRANSLATE_MODES, understand
from .product_text import load_product_text
from .public_catalogue import _public_offer, _safe_image_url, _visible
from .query_understanding import COLOURS, understand_photo
from .source_policy import SourcePolicy
from .storage import Database, offer_rows
from .taxonomy import fold
from .text_match import (
    TRANSLATIONS,
    matches_colour,
    matches_size,
    matches_terms,
    query_terms,
)
from .translation import get_translator
from .visual_encoder import (
    DEFAULT_ASPECT,
    FASHION_SIGLIP_SPACE,
    ImageEncoder,
    get_encoder,
    get_text_encoder,
    split_feature_version,
)

INDEX_FILE = "product-image-index.json"
# Version 2 stores each embedding as base64 little-endian float32, not a JSON list.
INDEX_VERSION = 2
MAX_ENTRIES = 20_000
RESULT_LIMIT = 5  # the release gate measures the top five; more is opt-in via ``limit``
MAX_RESULT_LIMIT = 50
LOOK_LIMIT = 20
CONSENSUS_NEIGHBOURS = 20
RRF_K = 60  # reciprocal rank fusion constant; Elasticsearch's default
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
# Re-ranking (colour boost, variant collapsing) looks at this many nearest offers;
# the rest keep their visual order.
RERANK_WINDOW = 200
ELIGIBLE_BATCH = 256  # photos checked per database round trip, nearest first
# Zero-shot category probability's share of the fused category vote. Product photos
# sit close to catalogue photos and their neighbours vote well; a street photo is far
# from all of them, so the model's own reading counts more. The weight moves from
# NEAR to FAR as the nearest offer's cosine similarity falls from 0.80 to 0.70
# (measured: packshots' nearest neighbours ≥ 0.72 in 95 %, street photos ≤ 0.75).
ZERO_SHOT_WEIGHT_NEAR = 0.25
ZERO_SHOT_WEIGHT_FAR = 0.75
NEAR_SIMILARITY = 0.80
FAR_SIMILARITY = 0.70
# Cosine-distance bonus for an offer whose colour matches a confidently seen colour.
COLOUR_BONUS = 0.02
# Two offers from one shop with the same title (colour words aside) are one product
# when their best photos are at least this similar, unless product groups say otherwise.
VARIANT_SIMILARITY = 0.80
_WORDS = re.compile(r"\w+")
_COLOUR_WORDS = frozenset(
    word
    for english in COLOURS
    for word in (english, *(fold(hungarian) for hungarian in TRANSLATIONS[english]))
)
logger = logging.getLogger(__name__)


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


def _with_preprocessing(encoder: ImageEncoder, aspect: str, flip_tta: bool) -> ImageEncoder:
    """The encoder with non-default preprocessing; only ONNX models offer it."""
    if aspect == DEFAULT_ASPECT and not flip_tta:
        return encoder
    if not hasattr(encoder, "with_options"):
        raise ValueError("aspect and flip_tta options need an ONNX image model")
    return encoder.with_options(aspect=aspect, flip_tta=flip_tta)


def build_image_index(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    manifest_path: Path,
    *,
    model_path: Path | None = None,
    aspect: str = DEFAULT_ASPECT,
    flip_tta: bool = False,
) -> dict[str, Any]:
    """Index every eligible manifest photo, then atomically replace the index.

    A malformed or unsafe manifest stops the build. A photo whose offer is no
    longer eligible is skipped and counted, so one sold-out item cannot block it.
    ``aspect`` and ``flip_tta`` (ONNX models only) choose the preprocessing; it is
    recorded in ``feature_version``, and photo search embeds queries the same way.
    """
    encoder = _with_preprocessing(get_encoder(model_path), aspect, flip_tta)
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
    path: str, inode: int, size: int, modified_ns: int
) -> tuple[str, tuple[dict[str, Any], ...], Any]:
    """Parse, validate and stack once per index file; a rebuild changes the cache key.

    Returns the stored feature version, the entries and their matrix (None without
    NumPy). Callers check the feature version against their encoder.
    """
    index = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = index["entries"]
    feature_version = index["feature_version"]
    if (
        index["version"] != INDEX_VERSION
        or not isinstance(feature_version, str)
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= MAX_ENTRIES
    ):
        raise ValueError("image index is incompatible")
    length = None
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
        length = len(values) if length is None else length
        if len(values) != length or not all(math.isfinite(value) for value in values):
            raise ValueError("image index contains invalid entries")
        entry["features"] = values
    for row, entry in enumerate(entries):
        entry["_row"] = row  # position in the cached matrix
    return feature_version, tuple(entries), _stack(entries, length)


def _index_contents(database: Database) -> tuple[str, tuple[dict[str, Any], ...], Any]:
    path = database.path.parent / INDEX_FILE
    try:
        stat = path.stat()
        return _read_index(str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, binascii.Error) as exc:
        raise ValueError("image index is unavailable or invalid") from exc


def _base_version(version: str) -> str:
    try:
        return split_feature_version(version)[0]
    except ValueError as exc:
        raise ValueError("image index is incompatible") from exc


def _load_index(
    database: Database, encoder: ImageEncoder
) -> tuple[tuple[dict[str, Any], ...], Any]:
    """The index, if it was built with the encoder's model.

    Any preprocessing variant of that model qualifies: text-to-photo search and
    readiness only need the same embedding space. Photo search uses
    :func:`_photo_index`, which also requires the same preprocessing.
    """
    feature_version, entries, matrix = _index_contents(database)
    if _base_version(feature_version) != _base_version(encoder.version) or (
        len(entries[0]["features"]) != encoder.length
    ):
        raise ValueError("image index is incompatible")
    return entries, matrix


def _photo_index(
    database: Database, base: ImageEncoder, aspect: str | None, flip_tta: bool | None
) -> tuple[ImageEncoder, tuple[dict[str, Any], ...], Any]:
    """The encoder variant the index was built with, and the index itself.

    ``aspect`` or ``flip_tta`` left as None follow the index; a value that differs
    from how the index was built is refused rather than silently mixed.
    """
    feature_version, entries, matrix = _index_contents(database)
    _model, stored_aspect, stored_flip = split_feature_version(feature_version)
    if (aspect is not None and aspect != stored_aspect) or (
        flip_tta is not None and flip_tta != stored_flip
    ):
        raise ValueError(
            "image index was built with other preprocessing "
            f"(aspect={stored_aspect}, flip_tta={stored_flip}); rebuild it to compare"
        )
    encoder = _with_preprocessing(base, stored_aspect, stored_flip)
    if feature_version != encoder.version or len(entries[0]["features"]) != encoder.length:
        raise ValueError("image index is incompatible")
    return encoder, entries, matrix


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
    # An offer's photos share its visibility, public payload, stored URLs and filter
    # outcome, so each is worked out once per offer; the per-photo checks below
    # (policy version, both URL lists) still run for every photo.
    checked: dict[int, tuple[SourcePolicy, dict[str, Any], set[str]] | None] = {}
    for entry in entries:
        row = rows.get(entry["offer_id"])
        if row is None or row["source_key"] == "demo" or row["source_key"] != entry["source_key"]:
            continue
        if row["id"] not in checked:
            checked[row["id"]] = _checked_offer(
                row,
                policies,
                current,
                source=source,
                category=category,
                colour=colour,
                size=size,
                max_price=max_price,
            )
        found = checked[row["id"]]
        if found is None:
            continue
        policy, offer, stored_urls = found
        if (
            policy.policy_version != entry["policy_version"]
            or entry["image_url"] not in stored_urls
            or entry["image_url"] not in offer["image_urls"]
        ):
            continue
        matches.append((entry, offer))
    return matches


def _checked_offer(
    row: dict[str, Any],
    policies: dict[tuple[str, str], SourcePolicy],
    current: datetime,
    *,
    source: str | None,
    category: str | None,
    colour: str | None,
    size: str | None,
    max_price: Decimal | None,
) -> tuple[SourcePolicy, dict[str, Any], set[str]] | None:
    """The offer's policy, public payload and stored photo URLs, or None if it is out."""
    policy = _visible(row, policies, current)
    if policy is None or not policy.allow_image_display or not policy.allow_image_processing:
        return None
    offer = _public_offer(row, policy)
    if (
        (source and offer["source_key"] != source)
        or (category and offer["category"] != category)
        or (colour and not matches_colour(colour, offer["colour_names"]))
        or (size and not matches_size(size, offer["size_options"]))
        or (max_price is not None and Decimal(offer["price_amount"]) > max_price)
    ):
        return None
    return policy, offer, set(json.loads(row["image_urls"]))


def _nearest_eligible(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    encoder: ImageEncoder,
    query: list[float],
    entries: tuple[dict[str, Any], ...],
    matrix,
    *,
    offers_needed: int,
    **filters: Any,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Eligible (entry, offer) pairs, nearest photos first, until enough offers are found.

    Ranking only ever looks at the nearest ``RERANK_WINDOW`` offers, so checking
    every photo's offer against policy and filters on each search is wasted work;
    the same checks run, in batches, only as far down the ranking as needed.
    """
    distances = _distances(encoder, query, list(entries), matrix)
    order = sorted(range(len(entries)), key=lambda i: (distances[i], entries[i]["offer_id"]))
    batch = max(4 * offers_needed, ELIGIBLE_BATCH)
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    found: set[int] = set()
    for start in range(0, len(order), batch):
        chunk = [entries[i] for i in order[start : start + batch]]
        for entry, offer in _eligible(database, policies, chunk, **filters):
            candidates.append((entry, offer))
            found.add(offer["id"])
        if len(found) >= offers_needed:
            break
    return candidates


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


def category_shares(offers: list[dict[str, Any]]) -> dict[str, float]:
    """Each category's share of the rank-weighted vote of the closest offers.

    Weights fall as 1, 1/2, 1/3, … so the nearest matches count most; offers without
    a category still count towards the total.
    """
    votes: Counter[str] = Counter()
    total = 0.0
    for position, offer in enumerate(offers[:CONSENSUS_NEIGHBOURS]):
        weight = 1 / (position + 1)
        total += weight
        if offer.get("category"):
            votes[offer["category"]] += weight
    return {category: weight / total for category, weight in votes.items()}


def zero_shot_weight(nearest_similarity: float) -> float:
    """How much the photo's own zero-shot reading counts against the neighbours' vote."""
    if nearest_similarity >= NEAR_SIMILARITY:
        return ZERO_SHOT_WEIGHT_NEAR
    if nearest_similarity <= FAR_SIMILARITY:
        return ZERO_SHOT_WEIGHT_FAR
    share = (NEAR_SIMILARITY - nearest_similarity) / (NEAR_SIMILARITY - FAR_SIMILARITY)
    return ZERO_SHOT_WEIGHT_NEAR + share * (ZERO_SHOT_WEIGHT_FAR - ZERO_SHOT_WEIGHT_NEAR)


def consensus_category(
    offers: list[dict[str, Any]],
    zero_shot: dict[str, float] | None = None,
    weight: float = ZERO_SHOT_WEIGHT_NEAR,
) -> str | None:
    """Return the category holding a majority of the closest offers' weighted vote.

    With ``zero_shot`` (the model's category probabilities for the photo itself),
    the vote and those probabilities are blended first, the probabilities counting
    ``weight``; a photo the model clearly reads as a jacket can then tip a split
    vote. Without a clear majority the visual order is kept unchanged.
    """
    shares = category_shares(offers)
    if zero_shot:
        shares = {
            name: (1 - weight) * shares.get(name, 0.0) + weight * zero_shot.get(name, 0.0)
            for name in shares.keys() | zero_shot.keys()
        }
    if not shares:
        return None
    category, share = max(shares.items(), key=lambda item: (item[1], item[0]))
    return category if share > 0.5 else None


def _scored(
    encoder: ImageEncoder,
    query: list[float],
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
    matrix=None,
) -> list[tuple[float, dict[str, Any], dict[str, Any]]]:
    """(distance, offer, best photo entry) per distinct offer, nearest first."""
    distances = _distances(encoder, query, [entry for entry, _ in candidates], matrix)
    order = sorted(range(len(candidates)), key=lambda i: (distances[i], candidates[i][1]["id"]))
    scored = []
    seen: set[int] = set()
    for position in order:
        entry, offer = candidates[position]
        if offer["id"] not in seen:
            seen.add(offer["id"])
            scored.append((distances[position], offer, entry))
    return scored


def _visual_order(
    encoder: ImageEncoder,
    query: list[float],
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
    matrix=None,
) -> list[dict[str, Any]]:
    """Distinct offers, nearest first; an offer's best photo decides its place."""
    return [offer for _, offer, _ in _scored(encoder, query, candidates, matrix)]


def _boost_colour(
    scored: list[tuple[float, dict[str, Any], dict[str, Any]]], colour: str
) -> list[tuple[float, dict[str, Any], dict[str, Any]]]:
    """Move offers in a colour the model saw clearly a little closer."""
    terms = query_terms(colour)
    boosted = [
        (
            distance
            - (
                COLOUR_BONUS
                if any(matches_terms(terms, name) for name in offer.get("colour_names") or ())
                else 0.0
            ),
            offer,
            entry,
        )
        for distance, offer, entry in scored
    ]
    return sorted(boosted, key=lambda item: (item[0], item[1]["id"]))


def _title_key(title: str | None) -> str:
    """A title without accents, case or colour words: "Kabát - fekete" → "kabat"."""
    return " ".join(word for word in _WORDS.findall(fold(title or "")) if word not in _COLOUR_WORDS)


def _variant_key(offer: dict[str, Any]) -> tuple[Any, ...] | None:
    """Offers that may be one product share this key; None never merges.

    A product-group id decides when the offer has one. Otherwise the offers must
    come from the same shop at the same price and share a title once colour words
    are dropped; a cheaper colour, or the same item at another shop, stays visible.
    """
    if offer.get("product_group_id") is not None:
        return ("group", offer["product_group_id"])
    title = _title_key(offer.get("title"))
    if not title:
        return None
    return ("title", offer.get("source_key"), offer.get("price_amount"), title)


def _collapse_variants(
    encoder: ImageEncoder, scored: list[tuple[float, dict[str, Any], dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Fold each product's colour variants and duplicate listings into its nearest offer.

    Offers with the same product-group id merge. Without one, offers with the same
    :func:`_variant_key` merge only when their best photos are the same file or at
    least ``VARIANT_SIMILARITY`` alike: photo similarity alone is not enough, because
    shops reuse one outfit photo on the jacket's and the trousers' pages. The kept
    offer gains ``variant_count`` (how many offers it absorbed) and
    ``variant_colours`` (their colour names that it does not list itself).
    """
    gram = None
    if encoder.cosine and scored:
        matrix = _stack([entry for _, _, entry in scored], len(scored[0][2]["features"]))
        gram = None if matrix is None else matrix @ matrix.T
    buckets: dict[tuple[Any, ...], list[int]] = {}
    members: dict[int, list[dict[str, Any]]] = {}
    for position, (_, offer, entry) in enumerate(scored):
        key = _variant_key(offer)
        home = None
        for index in buckets.get(key, []) if key else []:
            other = scored[index][2]
            if (
                key[0] == "group"
                or (entry.get("sha256") and entry.get("sha256") == other.get("sha256"))
                or (gram is not None and gram[position, index] >= VARIANT_SIMILARITY)
            ):
                home = index
                break
        if home is None:
            members[position] = []
            if key:
                buckets.setdefault(key, []).append(position)
        else:
            members[home].append(offer)
    offers = []
    for index, absorbed in members.items():
        offer = scored[index][1]
        if absorbed:
            own = {fold(name) for name in offer.get("colour_names") or ()}
            colours = {
                fold(name): name
                for member in absorbed
                for name in member.get("colour_names") or ()
                if fold(name) not in own
            }
            offer = {
                **offer,
                "variant_count": len(absorbed),
                "variant_colours": sorted(colours.values(), key=fold),
            }
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
    diversify: bool = False,
    zero_shot: dict[str, float] | None = None,
    colour: str | None = None,
) -> dict[str, Any]:
    scored = _scored(encoder, query, candidates, matrix)
    window, rest = scored[:RERANK_WINDOW], scored[RERANK_WINDOW:]
    if colour:
        window = _boost_colour(window, colour)
    offers = _collapse_variants(encoder, window) if diversify else [offer for _, offer, _ in window]
    offers += [offer for _, offer, _ in rest]
    result: dict[str, Any] = {"items": [], "next_cursor": None}
    weight = zero_shot_weight(1.0 - scored[0][0]) if scored and encoder.cosine else 0.0
    preferred = consensus_category(offers, zero_shot, weight) if consensus else None
    if preferred and offers:
        # A red-dress photo should list dresses before red bags, but the single
        # nearest match always stays first; the sort is stable.
        rest_offers = sorted(offers[1:], key=lambda offer: offer["category"] != preferred)
        offers = [offers[0], *rest_offers]
        result["preferred_category"] = preferred
    result["items"] = offers[:limit]
    return result


def _understanding_text_encoder(
    encoder: ImageEncoder, text_model_path: Path | str | None, tokenizer_path: Path | str | None
):
    """FashionSigLIP's text tower for auto-crop and zero-shot, or None.

    The colour-layout descriptor and the ViT have no paired text model, so both
    features stay off for them, as they do when no text model is configured.
    """
    if encoder.space != FASHION_SIGLIP_SPACE or not text_model_path or not tokenizer_path:
        return None
    try:
        return get_text_encoder(Path(text_model_path), Path(tokenizer_path))
    except ValueError as exc:
        logger.warning("photo auto-crop and zero-shot are off: %s", exc)
        return None


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
    model_path: Path | str | None = None,
    consensus: bool = True,
    text_model_path: Path | str | None = None,
    tokenizer_path: Path | str | None = None,
    manual_crop: bool = False,
    auto_crop: bool = True,
    zero_shot: bool = True,
    aspect: str | None = None,
    flip_tta: bool | None = None,
    diversify: bool = True,
    limit: int = RESULT_LIMIT,
) -> dict[str, Any]:
    """Rank indexed product photos by how much they look like the shopper's photo.

    Every option has a safe default and can be switched off to compare variants:

    - ``auto_crop``: search with the region that looks most like one garment, and
      list other garments as ``query.alternatives``. Off when ``manual_crop`` says
      the shopper already cropped the photo.
    - ``zero_shot``: read the photo's category and colour with text prompts; blend
      the category into the neighbours' vote and move offers in a confidently seen
      colour slightly up. Fills ``query.category`` and ``query.colour``.
      Both need Marqo-FashionSigLIP with ``text_model_path`` and ``tokenizer_path``;
      without them, and with the colour-layout or ViT encoders, they are off and
      the whole photo is embedded as before.
    - ``aspect`` and ``flip_tta``: None follows how the index was built (see
      :func:`build_image_index`); a different explicit value raises ValueError.
    - ``diversify``: fold colour variants and duplicate listings of one product
      into its nearest offer, which gains ``variant_count``.
    - ``consensus``: the 20 nearest offers vote on a category listed first.
    - ``limit``: results returned, 1–50; the release gate measures the default 5.
    """
    if not 1 <= limit <= MAX_RESULT_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_RESULT_LIMIT}")
    base = get_encoder(Path(model_path) if model_path else None)
    encoder, entries, matrix = _photo_index(database, base, aspect, flip_tta)
    text_encoder = (
        _understanding_text_encoder(encoder, text_model_path, tokenizer_path)
        if auto_crop or zero_shot
        else None
    )
    understood = understand_photo(
        encoder,
        text_encoder,
        image,
        auto_crop=auto_crop,
        zero_shot=zero_shot,
        manual_crop=manual_crop,
        nearest=(lambda vector: float((matrix @ vector).max())) if matrix is not None else None,
    )
    query = understood.as_json()
    candidates = _nearest_eligible(
        database,
        policies,
        encoder,
        understood.vector,
        entries,
        matrix,
        offers_needed=max(RERANK_WINDOW, limit),
        source=source,
        category=category,
        colour=colour,
        size=size,
        max_price=max_price,
    )
    result = _rank(
        encoder,
        understood.vector,
        candidates,
        consensus=consensus,
        matrix=matrix,
        limit=limit,
        diversify=diversify,
        zero_shot=understood.category_probabilities if zero_shot else None,
        # A shopper's own colour filter already decides; otherwise use what the model saw.
        colour=None if colour or not query["colour"] else query["colour"]["value"],
    )
    result["query"] = query
    return result


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
    translate: str = "dictionary",
    translation_model_dir: Path | None = None,
    price_hints: bool = True,
    prompt_template: str | list[str] | None = look_search.DEFAULT_PROMPT_TEMPLATE,
    product_text_weight: float | None = None,
    hints: str = "boost",
    fusion: str = "rrf",
    rrf_k: float = RRF_K,
    lexical_weight: float = 1.0,
    hint_weight: float = look_search.DEFAULT_HINT_WEIGHT,
) -> dict[str, Any]:
    """Rank indexed product photos by how well they match a description.

    The model's text tower only understands English, so the query is read first
    (``look_query.understand``): Hungarian words are translated, a price phrase
    such as ``10000 Ft alatt`` becomes a price limit, and colour, category and
    gender become hints. The response's ``interpreted_query`` is the English text
    actually embedded. Options, each with a safe default:

    - ``translate``: ``"dictionary"`` (default), ``"dictionary+mt"`` (the offline
      translation model for words the dictionary misses; needs
      ``translation_model_dir``), ``"mt"`` (the model for the whole query) or
      ``"none"`` (embed the words as typed).
    - ``price_hints``: read price and size phrases from the text (default on). An
      explicit ``max_price`` wins over a typed one.
    - ``prompt_template``: wrap the query, e.g. ``"a photo of {q}"``; a list of
      templates averages their embeddings; None embeds the bare query.
    - ``product_text_weight``: 0–1 share of the product-text sidecar similarity in
      the visual score. None (default) uses the measured default when a compatible
      sidecar exists; an explicit weight above 0 requires one.
    - ``blend``: fuse the visual order with word relevance and hints (default on);
      False ranks by look alone.
    - ``hints``: ``"boost"`` (default: an offer meeting all of the query's colour,
      category, gender and size hints gains ``hint_weight`` standard deviations of
      visual score, a share for a share of them),
      ``"filter"`` (keep the hinted category and colour when any offer has them)
      or ``"off"``.
    - ``fusion``: ``"rrf"`` (default; reciprocal rank fusion of the visual order and
      word relevance, constant ``rrf_k``) or ``"weighted"`` (standardised visual
      score plus ``lexical_weight`` times the normalised word relevance).
    """
    if not text.strip():
        raise ValueError("describe the look to search for")
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    if translate not in TRANSLATE_MODES:
        raise ValueError(f"translate must be one of {', '.join(TRANSLATE_MODES)}")
    translator = get_translator(translation_model_dir) if "mt" in translate else None
    look = understand(text, translate=translate, translator=translator, price_hints=price_hints)
    if not look.english.strip():
        raise ValueError("describe the look to search for")
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
        max_price=max_price if max_price is not None else look.max_price,
    )
    if look.min_price is not None:
        candidates = [
            (entry, offer)
            for entry, offer in candidates
            if Decimal(offer["price_amount"]) >= look.min_price
        ]
    sidecar = None
    weight = product_text_weight
    if weight is None:
        try:
            sidecar = load_product_text(database, image_encoder, text_encoder)
        except ValueError:
            sidecar = None  # a stale sidecar never blocks look search
        weight = look_search.DEFAULT_PRODUCT_TEXT_WEIGHT if sidecar else 0.0
    elif weight > 0:
        sidecar = load_product_text(database, image_encoder, text_encoder)
        if sidecar is None:
            raise ValueError("product_text_weight needs a product text index (index-product-text)")
    vector = look_search.embed(text_encoder, look_search.templated(look.english, prompt_template))
    items = look_search.rank(
        look,
        vector,
        image_encoder,
        candidates,
        matrix,
        product_text=sidecar,
        product_text_weight=weight,
        blend=blend,
        fusion=fusion,
        rrf_k=rrf_k,
        lexical_weight=lexical_weight,
        hints=hints,
        hint_weight=hint_weight,
    )
    return {
        "items": items[:limit],
        "next_cursor": None,
        "interpreted_query": look.english,
        "interpretation": look.as_response(),
    }

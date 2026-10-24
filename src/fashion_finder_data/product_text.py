"""Product text embeddings: a sidecar index of short English product descriptions.

Look search compares a description with product photos. When a photo is
ambiguous (a model wearing a coat over a dress), the offer's own words help:
each indexed offer gets a short English description built from its structured
fields, embedded with the same text tower, and stored in its own file next to
the photo index. The sidecar is never mixed into the photo index.
"""

from __future__ import annotations

import binascii
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .hungarian import analyse
from .public_catalogue import _visible
from .source_policy import SourcePolicy
from .storage import Database, offer_rows

PRODUCT_TEXT_FILE = "product-text-index.json"
PRODUCT_TEXT_VERSION = 1
# Bump when `describe` changes, so a sidecar built the old way is refused.
DESCRIPTION_VERSION = "describe-v1"
MAX_DESCRIPTION_WORDS = 24
CATEGORY_NOUNS = {
    "coats": "coat",
    "jackets": "jacket",
    "dresses": "dress",
    "sweaters": "sweater",
    "shirts": "shirt",
    "blouses": "blouse",
    "tops": "top",
    "trousers": "trousers",
    "jeans": "jeans",
    "skirts": "skirt",
    "shorts": "shorts",
    "shoes": "shoes",
    "bags": "bag",
    "accessories": "accessory",
}
_DESCRIPTIVE = {"garment", "colour", "material", "pattern", "style", "machine", "other"}
_STOP = {"a", "an", "and", "of", "with", "for", "in", "on", "the", "without"}
_DETAIL = {"material", "pattern"}


def offer_digest(title: str, category: str | None, colours: list[str]) -> str:
    """What a description depends on; a changed offer no longer matches its entry."""
    return _digest(title, category, tuple(colours))


@lru_cache(maxsize=65_536)
def _digest(title: str, category: str | None, colours: tuple[str, ...]) -> str:
    payload = json.dumps([title, category, list(colours)], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def describe(row: dict[str, Any]) -> str:
    """A short English description: ``women's black quilted hooded coat``.

    Built from the title, colours and category, plus materials and patterns named
    in the source category or description. Unknown words (brands, typos) are left
    out, because the model would read them as noise.
    """
    title = analyse(str(row.get("title") or ""))
    colours = json.loads(row["colour_names"]) if isinstance(row.get("colour_names"), str) else []
    words: list[str] = []
    words += [word.english for word in title if word.kind == "gender"][:1]
    for colour in colours[:2]:
        words += [word.english for word in analyse(colour) if word.kind == "colour"][:1]
    words += [word.english for word in title if word.kind in _DESCRIPTIVE]
    extra = f"{row.get('source_category') or ''} {row.get('description') or ''}"
    words += [word.english for word in analyse(extra[:400]) if word.kind in _DETAIL][:4]
    noun = CATEGORY_NOUNS.get(row.get("category") or "")
    if noun and not any(word.category == row.get("category") for word in title):
        words.append(noun)
    seen: set[str] = set()
    unique = []
    for word in " ".join(words).split():
        if word.casefold() not in seen and word.casefold() not in _STOP:
            seen.add(word.casefold())
            unique.append(word)
    return " ".join(unique[:MAX_DESCRIPTION_WORDS])


def build_product_text_index(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    *,
    model_path: Path | None = None,
    text_model_path: Path | None = None,
    tokenizer_path: Path | None = None,
) -> dict[str, Any]:
    """Describe and embed every currently visible offer in the photo index."""
    from . import image_index

    image_encoder = image_index.get_encoder(model_path)
    text_encoder = image_index._text_encoder_for(image_encoder, text_model_path, tokenizer_path)
    entries, _ = image_index._load_index(database, image_encoder)
    rows = offer_rows(database, id__in=sorted({entry["offer_id"] for entry in entries}))
    now = datetime.now(UTC)
    indexed = []
    skipped: Counter[str] = Counter()
    for row in sorted(rows, key=lambda item: item["id"]):
        if _visible(row, policies, now) is None:
            skipped["offer is not currently visible"] += 1
            continue
        description = describe(row)
        if not description:
            skipped["no describable words"] += 1
            continue
        indexed.append(
            {
                "offer_id": row["id"],
                "digest": offer_digest(
                    row["title"], row["category"], json.loads(row["colour_names"])
                ),
                "description": description,
                "features": image_index.pack_features(text_encoder.encode(description)),
            }
        )
    if not indexed:
        raise ValueError(f"no offers to describe; skipped: {dict(skipped)}")
    destination = database.path.parent / PRODUCT_TEXT_FILE
    temporary = destination.with_name(f".{PRODUCT_TEXT_FILE}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "version": PRODUCT_TEXT_VERSION,
                "description_version": DESCRIPTION_VERSION,
                "text_version": text_encoder.version,
                "image_feature_version": image_encoder.version,
                "entries": indexed,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(destination)
    return {
        "status": "indexed" if not skipped else "partial",
        "described_offers": len(indexed),
        "skipped_offers": dict(skipped),
        "text_version": text_encoder.version,
        "index_path": str(destination),
    }


@dataclass(frozen=True)
class ProductText:
    """Loaded sidecar: offer id -> (digest, row in ``matrix``)."""

    rows: dict[int, tuple[str, int]]
    vectors: tuple[Any, ...]
    matrix: Any  # float32 NumPy matrix, or None without NumPy

    def similarities(self, query: list[float], offers: list[dict[str, Any]]) -> list[float | None]:
        """Cosine similarity per offer; None when the offer has no current entry."""
        wanted = []
        for offer in offers:
            found = self.rows.get(offer["id"])
            current = offer_digest(offer["title"], offer["category"], offer["colour_names"])
            wanted.append(found[1] if found and found[0] == current else None)
        if self.matrix is not None:
            import numpy as np

            scores = self.matrix @ np.asarray(query, dtype=np.float32)
            return [None if row is None else float(scores[row]) for row in wanted]
        return [
            None if row is None else sum(a * b for a, b in zip(query, self.vectors[row]))
            for row in wanted
        ]


@lru_cache(maxsize=2)
def _read(
    path: str,
    inode: int,
    size: int,
    modified_ns: int,
    text_version: str,
    image_version: str,
    length: int,
) -> ProductText:
    from .image_index import MAX_ENTRIES, unpack_features

    sidecar = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = sidecar["entries"]
    if (
        sidecar["version"] != PRODUCT_TEXT_VERSION
        or sidecar["description_version"] != DESCRIPTION_VERSION
        or sidecar["text_version"] != text_version
        or sidecar["image_feature_version"] != image_version
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= MAX_ENTRIES
    ):
        raise ValueError("product text index is incompatible; rebuild it with index-product-text")
    rows: dict[int, tuple[str, int]] = {}
    vectors = []
    for entry in entries:
        if not isinstance(entry["offer_id"], int) or not isinstance(entry["digest"], str):
            raise TypeError("product text index contains invalid entries")
        values = unpack_features(entry["features"])
        if len(values) != length or not all(math.isfinite(value) for value in values):
            raise ValueError("product text index contains invalid entries")
        rows[entry["offer_id"]] = (entry["digest"], len(vectors))
        vectors.append(values)
    try:
        import numpy as np
    except ImportError:
        matrix = None
    else:
        matrix = np.frombuffer(
            b"".join(vector.tobytes() for vector in vectors), dtype=np.float32
        ).reshape(len(vectors), length)
    return ProductText(rows, tuple(vectors), matrix)


def load_product_text(database: Database, image_encoder, text_encoder) -> ProductText | None:
    """The sidecar for this photo index and text model; None when none was built.

    A sidecar from another text model, photo index or description version raises,
    so similarities from two embedding versions are never mixed.
    """
    path = database.path.parent / PRODUCT_TEXT_FILE
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("product text index is unavailable") from exc
    try:
        return _read(
            str(path),
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            getattr(text_encoder, "version", ""),
            image_encoder.version,
            text_encoder.length,
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, binascii.Error) as exc:
        raise ValueError("product text index is unavailable or invalid") from exc

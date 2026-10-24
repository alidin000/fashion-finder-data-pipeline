"""Retrieval benchmark on the practice catalogue: tasks, metrics and two rankers.

The embedding ranker (Script B in docs/data-research/public-datasets.md) scores raw
model embeddings, so any pinned encoder can be compared. The real ranker (Harness A)
loads the practice catalogue into a temporary data directory with the queries held
out and calls the shipped search functions, so their options can be compared too.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import resource
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any

from PIL import Image

from ..ranking_evaluation import ranking_metrics
from ..visual_features import FEATURE_VERSION
from .normalise import PracticeProduct, stable_fraction

HELD_OUT_PER_CATEGORY = 15  # GLAMI products kept out of the gallery as category queries
SAMPLE_PER_CATEGORY = 15
MAX_PHRASES = 150
MAX_NEW_ENCODINGS = 2000

NOUNS = {
    "coats": ("kabát", "coat"),
    "jackets": ("dzseki", "jacket"),
    "dresses": ("ruha", "dress"),
    "sweaters": ("pulóver", "sweater"),
    "shirts": ("ing", "shirt"),
    "blouses": ("blúz", "blouse"),
    "tops": ("póló", "t-shirt"),
    "trousers": ("nadrág", "trousers"),
    "jeans": ("farmer", "jeans"),
    "skirts": ("szoknya", "skirt"),
    "shorts": ("rövidnadrág", "shorts"),
    "shoes": ("cipő", "shoes"),
    "bags": ("táska", "bag"),
}
GENDERS = {"women": ("női", "women's"), "men": ("férfi", "men's"), "kids": ("gyerek", "kids'")}
COLOURS_HU = {
    "black": "fekete",
    "white": "fehér",
    "red": "piros",
    "blue": "kék",
    "navy": "sötétkék",
    "green": "zöld",
    "yellow": "sárga",
    "brown": "barna",
    "grey": "szürke",
    "beige": "bézs",
    "pink": "rózsaszín",
    "purple": "lila",
    "orange": "narancssárga",
    "cream": "krém",
    "burgundy": "bordó",
}
TASKS = ("a", "b", "c", "d_name", "d_phrase", "d_phrase_en")
PHOTO_TASKS = {"a", "b"}


@dataclass(frozen=True)
class Query:
    task: str
    query_id: str
    relevant: frozenset[str]  # practice IDs
    photo: str | None = None  # catalogue photo path
    text: str | None = None
    category: str | None = None


@dataclass
class Split:
    """Which products and photos form the gallery, and the queries asked of it."""

    products: dict[str, PracticeProduct]
    gallery: list[str]  # practice IDs
    gallery_photos: list[tuple[str, str]]  # (catalogue photo path, practice ID)
    held_out_products: frozenset[str]
    held_out_photos: frozenset[str]
    queries: dict[str, list[Query]] = field(default_factory=dict)

    def sizes(self) -> dict[str, Any]:
        return {
            "catalogue_products": len(self.products),
            "gallery_products": len(self.gallery),
            "gallery_photos": len(self.gallery_photos),
            "held_out_products": len(self.held_out_products),
            "held_out_photos": len(self.held_out_photos),
            "queries": {task: len(items) for task, items in self.queries.items()},
        }


def _pick(items: Iterable[PracticeProduct], count: int, salt: str) -> list[PracticeProduct]:
    return sorted(items, key=lambda item: stable_fraction(salt, item.practice_id))[:count]


def _by_category(products: Iterable[PracticeProduct]) -> dict[str, list[PracticeProduct]]:
    grouped: dict[str, list[PracticeProduct]] = defaultdict(list)
    for product in products:
        grouped[product.category].append(product)
    return grouped


def build_split(products: list[PracticeProduct]) -> Split:
    """Deterministic held-out queries; nothing a query asks about leaks into the gallery."""
    catalogue = {product.practice_id: product for product in products}
    glami = _by_category(product for product in products if product.dataset == "glami")
    held_products = {
        product.practice_id
        for items in glami.values()
        if len(items) >= 2 * HELD_OUT_PER_CATEGORY
        for product in _pick(items, HELD_OUT_PER_CATEGORY, "bench-holdout")
    }
    multi_view = [
        product for product in products if product.dataset == "abo" and len(product.images) >= 2
    ]
    held_photos = {product.images[-1].path: product.practice_id for product in multi_view}
    gallery = [pid for pid in catalogue if pid not in held_products]
    gallery_photos = [
        (image.path, pid)
        for pid in gallery
        for image in catalogue[pid].images
        if image.path not in held_photos
    ]
    split = Split(
        catalogue, gallery, gallery_photos, frozenset(held_products), frozenset(held_photos)
    )
    in_gallery = [catalogue[pid] for pid in gallery]
    category_members = _by_category(in_gallery)

    def same_category(category: str) -> frozenset[str]:
        return frozenset(product.practice_id for product in category_members[category])

    split.queries["a"] = [
        Query(
            "a",
            f"a:{product.practice_id}",
            frozenset({product.practice_id}),
            photo=path,
            category=product.category,
        )
        for path, pid in held_photos.items()
        for product in (catalogue[pid],)
    ]
    abo_queries = _by_category(catalogue[pid] for pid in held_photos.values())
    category_queries = [
        (product, product.images[-1].path)
        for items in abo_queries.values()
        for product in _pick(items, SAMPLE_PER_CATEGORY, "bench-b")
    ] + [(catalogue[pid], catalogue[pid].images[0].path) for pid in sorted(held_products)]
    split.queries["b"] = [
        Query(
            "b",
            f"b:{product.practice_id}",
            same_category(product.category),
            photo=path,
            category=product.category,
        )
        for product, path in category_queries
    ]
    split.queries["c"] = [
        Query("c", f"c:{product.practice_id}", frozenset({product.practice_id}), text=product.title)
        for product in in_gallery
        if product.language == "en"
    ]
    glami_gallery = _by_category(product for product in in_gallery if product.language == "hu")
    split.queries["d_name"] = [
        Query(
            "d_name",
            f"d:{product.practice_id}",
            frozenset({product.practice_id}),
            text=product.title,
        )
        for items in glami_gallery.values()
        for product in _pick(items, SAMPLE_PER_CATEGORY, "bench-d")
    ]
    phrases = []
    groups: dict[tuple[str, str | None, str], set[str]] = defaultdict(set)
    for product in in_gallery:
        if product.colour and product.category in NOUNS:
            groups[(product.category, product.gender, product.colour)].add(product.practice_id)
            groups[(product.category, None, product.colour)].add(product.practice_id)
    for (category, gender, colour), members in sorted(
        groups.items(), key=lambda item: stable_fraction("bench-phrase", repr(item[0]))
    ):
        if len(members) < (3 if gender else 5):
            continue
        hungarian = " ".join(
            word
            for word in (
                COLOURS_HU[colour],
                GENDERS[gender][0] if gender else None,
                NOUNS[category][0],
            )
            if word
        )
        english = " ".join(
            word
            for word in (colour, GENDERS[gender][1] if gender else None, NOUNS[category][1])
            if word
        )
        phrases.append((hungarian, english, frozenset(members), category))
        if len(phrases) >= MAX_PHRASES:
            break
    split.queries["d_phrase"] = [
        Query("d_phrase", f"p:{hu}", members, text=hu, category=category)
        for hu, _en, members, category in phrases
    ]
    split.queries["d_phrase_en"] = [
        Query("d_phrase_en", f"p:{en}", members, text=en, category=category)
        for _hu, en, members, category in phrases
    ]
    return split


def query_metrics(ranked: list[str], relevant: frozenset[str]) -> dict[str, float]:
    """Hit@5, MRR@10 and nDCG@10 from ranking_evaluation, plus Hit@1, Recall@10, P@10."""
    order = list(dict.fromkeys(ranked))
    numbers = {pid: number for number, pid in enumerate(order, start=1)}
    # Relevant products that were not ranked still count towards the ideal nDCG.
    labels = {numbers.get(pid, -number) for number, pid in enumerate(sorted(relevant), start=1)}
    found = sum(pid in relevant for pid in order[:10])
    return {
        "hit_at_1": float(bool(order) and order[0] in relevant),
        **ranking_metrics(list(range(1, len(order) + 1)), labels),
        "recall_at_10": found / len(relevant) if relevant else 0.0,
        "precision_at_10": found / 10,
    }


def percentile(values: list[float], share: float) -> float | None:
    """Nearest-rank percentile, so p95 of 20 values is the 19th."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def summarise(per_query: list[dict[str, float]], latencies: list[float]) -> dict[str, Any]:
    if not per_query:
        return {"queries": 0}
    summary = {
        "queries": len(per_query),
        **{key: round(mean(item[key] for item in per_query), 4) for key in per_query[0]},
        "latency_ms_p50": round(percentile(latencies, 0.5), 1) if latencies else None,
        "latency_ms_p95": round(percentile(latencies, 0.95), 1) if latencies else None,
    }
    # Category guesses: how often one was shown, and how often a shown one was right.
    for name in ("preferred_category", "query_category"):
        if f"{name}_shown" in summary:
            shown = sum(item[f"{name}_shown"] for item in per_query)
            right = sum(item[f"{name}_correct"] for item in per_query)
            summary[f"{name}_precision"] = round(right / shown, 4) if shown else None
    return summary


def category_guesses(result: dict[str, Any], category: str | None) -> dict[str, float]:
    """Score the vote's ``preferred_category`` and, once photo search reports it, the
    recognised ``query.category`` (agent 2's response field) against the true category."""
    query = result.get("query") or {}
    guesses = {
        "preferred_category": result.get("preferred_category"),
        "query_category": (query.get("category") or {}).get("value"),
    }
    scores: dict[str, float] = {}
    for name, value in guesses.items():
        scores[f"{name}_shown"] = float(value is not None)
        scores[f"{name}_correct"] = float(value is not None and value == category)
    return scores


# English prompts for zero-shot category recognition with the FashionSigLIP text tower.
ZERO_SHOT_PROMPTS = {
    "coats": "a photo of a coat",
    "jackets": "a photo of a jacket",
    "dresses": "a photo of a dress",
    "sweaters": "a photo of a sweater",
    "shirts": "a photo of a shirt",
    "blouses": "a photo of a blouse",
    "tops": "a photo of a t-shirt",
    "trousers": "a photo of trousers",
    "jeans": "a photo of jeans",
    "skirts": "a photo of a skirt",
    "shorts": "a photo of shorts",
    "shoes": "a photo of shoes",
    "bags": "a photo of a bag",
    "accessories": "a photo of jewellery or a fashion accessory",
}


def peak_rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def checked_options(function: Callable, raw: str | None, reserved: set[str]) -> dict[str, Any]:
    """Parse JSON keyword arguments and refuse any the search function does not accept."""
    if not raw:
        return {}
    try:
        options = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"options for {function.__name__} are not valid JSON: {exc}") from exc
    if not isinstance(options, dict):
        raise TypeError(f"options for {function.__name__} must be a JSON object")
    parameters = inspect.signature(function).parameters
    keyword = {
        name
        for name, parameter in parameters.items()
        if parameter.kind in (parameter.KEYWORD_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    }
    unsupported = sorted(set(options) - (keyword - reserved))
    if unsupported:
        raise ValueError(
            f"{function.__name__} does not accept {', '.join(unsupported)}; "
            f"options it accepts: {', '.join(sorted(keyword - reserved))}"
        )
    return options


# --- Embedding ranker (Script B) ------------------------------------------------------


def _open_photo(path: Path) -> Image.Image:
    with Image.open(path) as image:
        image.load()
        return image.copy()


class EmbeddingCache:
    """Embeddings per encoder version on disk, so runs resume and stay bounded."""

    def __init__(self, folder: Path, version: str):
        import numpy as np

        self.np = np
        key = hashlib.sha256(version.encode()).hexdigest()[:16]
        folder.mkdir(parents=True, exist_ok=True)
        self.matrix_path = folder / f"{key}.npy"
        self.paths_path = folder / f"{key}.json"
        (folder / f"{key}.version.txt").write_text(version + "\n", encoding="utf-8")
        if self.paths_path.exists():
            self.paths = json.loads(self.paths_path.read_text(encoding="utf-8"))
            self.matrix = np.load(self.matrix_path)
        else:
            self.paths, self.matrix = [], None
        self.rows = {path: row for row, path in enumerate(self.paths)}

    def missing(self, paths: Iterable[str]) -> list[str]:
        return [path for path in dict.fromkeys(paths) if path not in self.rows]

    def add(self, paths: list[str], vectors: list[list[float]]) -> None:
        np = self.np
        block = np.asarray(vectors, dtype=np.float32)
        self.matrix = block if self.matrix is None else np.vstack([self.matrix, block])
        self.paths.extend(paths)
        self.rows = {path: row for row, path in enumerate(self.paths)}
        temporary = self.matrix_path.with_name(self.matrix_path.stem + ".tmp.npy")
        np.save(temporary, self.matrix)
        temporary.replace(self.matrix_path)
        self.paths_path.write_text(json.dumps(self.paths), encoding="utf-8")

    def get(self, paths: list[str]):
        return self.matrix[[self.rows[path] for path in paths]]


def distance_matrix(encoder, queries, gallery):
    """Distances exactly as the product ranks them, vectorised for the benchmark."""
    import numpy as np

    if encoder.cosine:
        return 1.0 - queries @ gallery.T
    if encoder.version == FEATURE_VERSION:
        # visual_features.feature_distance: 0.7 * mean |Δpixels| + 0.3 * mean |Δhistogram|.
        rows = []
        for query in queries:
            delta = np.abs(gallery - query)
            rows.append(0.7 * delta[:, :768].mean(axis=1) + 0.3 * delta[:, 768:].mean(axis=1))
        return np.vstack(rows)
    return np.asarray([[encoder.distance(list(q), list(g)) for g in gallery] for q in queries])


def encode_photos(
    encoder, cache: EmbeddingCache, practice_dir: Path, paths: list[str], budget: int
) -> dict[str, Any]:
    """Encode at most ``budget`` uncached photos, saving every 250 so a stop loses little."""
    missing = cache.missing(paths)
    todo = missing[:budget]
    timings: list[float] = []
    for start in range(0, len(todo), 250):
        batch = todo[start : start + 250]
        vectors = []
        for path in batch:
            image = _open_photo(practice_dir / path)
            started = time.perf_counter()
            vectors.append(encoder.encode(image))
            timings.append((time.perf_counter() - started) * 1000)
        cache.add(batch, vectors)
    return {
        "encoded_now": len(todo),
        "still_missing": len(missing) - len(todo),
        "encode_ms_p50": round(percentile(timings, 0.5), 1) if timings else None,
        "encode_ms_p95": round(percentile(timings, 0.95), 1) if timings else None,
    }


def rank_products(distances, owners: list[str], limit: int = 50) -> list[str]:
    """Distinct products by their nearest photo, as image_index._visual_order does."""
    best: dict[str, float] = {}
    for distance, pid in zip(distances.tolist(), owners, strict=True):
        if distance < best.get(pid, math.inf):
            best[pid] = distance
    return sorted(best, key=lambda pid: (best[pid], pid))[:limit]


def run_embedding(
    split: Split,
    practice_dir: Path,
    cache_dir: Path,
    tasks: list[str],
    *,
    model_path: Path | None,
    text_model_path: Path | None,
    tokenizer_path: Path | None,
    max_new_encodings: int = MAX_NEW_ENCODINGS,
) -> dict[str, Any]:
    from ..visual_encoder import get_encoder, get_text_encoder

    encoder = get_encoder(model_path)
    cache = EmbeddingCache(cache_dir, encoder.version)
    needed = [path for path, _ in split.gallery_photos] + [
        query.photo for task in tasks if task in PHOTO_TASKS for query in split.queries[task]
    ]
    encoding = encode_photos(encoder, cache, practice_dir, needed, max_new_encodings)
    report: dict[str, Any] = {"encoder": encoder.version, "encoding": encoding, "tasks": {}}
    if encoding["still_missing"]:
        report["status"] = "partial: run again to encode the remaining photos"
        return report
    gallery = cache.get([path for path, _ in split.gallery_photos])
    owners = [pid for _, pid in split.gallery_photos]
    text_encoder = prompts = None
    if text_model_path and tokenizer_path:
        text_encoder = get_text_encoder(text_model_path, tokenizer_path)
        if text_encoder.space != encoder.space:
            raise ValueError("look tasks need a FashionSigLIP vision model")
        prompts = cache.np.asarray(
            [text_encoder.encode(prompt) for prompt in ZERO_SHOT_PROMPTS.values()],
            dtype=cache.np.float32,
        )
    for task in tasks:
        per_query, latencies = [], []
        for query in split.queries[task]:
            started = time.perf_counter()
            if query.photo:
                vector = cache.get([query.photo])
            else:
                vector = cache.np.asarray([text_encoder.encode(query.text)], dtype=cache.np.float32)
            ranked = rank_products(distance_matrix(encoder, vector, gallery)[0], owners)
            latencies.append((time.perf_counter() - started) * 1000)
            metrics = query_metrics(ranked, query.relevant)
            if query.photo and prompts is not None:
                guess = list(ZERO_SHOT_PROMPTS)[int((prompts @ vector[0]).argmax())]
                metrics["zero_shot_category"] = float(guess == query.category)
            per_query.append(metrics)
        report["tasks"][task] = summarise(per_query, latencies)
    report["status"] = "complete"
    return report


# --- Real ranker (Harness A) ----------------------------------------------------------

PHOTO_RESERVED = {"database", "policies", "image", "model_path"}
LOOK_RESERVED = {"database", "policies", "text", "model_path", "text_model_path", "tokenizer_path"}


def prepare_real_data_dir(
    split: Split, practice_dir: Path, data_dir: Path, model_path: Path | None
) -> dict[str, Any]:
    """Load the gallery through the real loader and build its index, one part per process."""
    from .loader import load_practice_catalogue

    loaded = load_practice_catalogue(
        data_dir,
        practice_dir,
        exclude_products=split.held_out_products,
        exclude_photos=split.held_out_photos,
    )
    code = (
        "import json, sys; from pathlib import Path; "
        "from fashion_finder_data.practice.loader import build_practice_index; "
        "model = Path(sys.argv[2]) if sys.argv[2] else None; "
        "print(json.dumps(build_practice_index(Path(sys.argv[1]), model_path=model)))"
    )
    for _ in range(50):
        result = subprocess.run(
            [sys.executable, "-c", code, str(data_dir), str(model_path or "")],
            check=True,
            capture_output=True,
            text=True,
        )
        index = json.loads(result.stdout.strip().splitlines()[-1])
        if index["index_status"] == "complete":
            return {"loaded": loaded, "index": index}
    raise RuntimeError("index build did not finish")


def run_real(
    split: Split,
    practice_dir: Path,
    data_dir: Path,
    tasks: list[str],
    *,
    model_path: Path | None,
    text_model_path: Path | None,
    tokenizer_path: Path | None,
    photo_options: dict[str, Any],
    look_options: dict[str, Any],
) -> dict[str, Any]:
    from ..image_index import search_indexed_image, search_indexed_text
    from ..source_policy import load_policies
    from ..storage import offer_rows, open_database
    from .loader import POLICY_FILE

    database = open_database(data_dir / "fashion_finder.sqlite")
    policies = load_policies(data_dir / POLICY_FILE)
    practice_ids = {
        row["id"]: row["source_product_id"]
        for row in offer_rows(database, retailer__source_key="practice", status="active")
    }
    report: dict[str, Any] = {"tasks": {}}
    for task in tasks:
        per_query, latencies = [], []
        for query in split.queries[task]:
            if query.photo:
                image = _open_photo(practice_dir / query.photo)
                started = time.perf_counter()
                result = search_indexed_image(
                    database, policies, image, model_path=model_path, **photo_options
                )
            else:
                started = time.perf_counter()
                result = search_indexed_text(
                    database,
                    policies,
                    query.text,
                    model_path=model_path,
                    text_model_path=text_model_path,
                    tokenizer_path=tokenizer_path,
                    **look_options,
                )
            latencies.append((time.perf_counter() - started) * 1000)
            ranked = [practice_ids[item["id"]] for item in result["items"]]
            metrics = query_metrics(ranked, query.relevant)
            if query.photo:
                metrics |= category_guesses(result, query.category)
            per_query.append(metrics)
        report["tasks"][task] = summarise(per_query, latencies)
    report["status"] = "complete"
    return report

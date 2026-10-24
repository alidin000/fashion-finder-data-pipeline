"""Rank eligible offers for a look description.

Four signals, each optional so the benchmark can compare them:
- photo similarity: the query embedding against each offer's best photo;
- product text similarity: the query against the offer's sidecar description;
- word relevance: the shopper's words (and their dictionary forms) in the offer's
  title, category and colours;
- hints: the colour, category and gender read from the query.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

from .look_query import LookQuery
from .product_text import ProductText
from .text_match import (
    Term,
    _words,
    matches_size,
    matches_terms,
    query_terms,
    relevance,
    row_text,
)

FUSIONS = ("rrf", "weighted")
HINT_MODES = ("off", "boost", "filter")
DEFAULT_PRODUCT_TEXT_WEIGHT = 0.5  # measured; see docs
DEFAULT_HINT_WEIGHT = 0.5
HINT_DEPTH = 200  # the hint bonus is computed for offers that can reach these places
DEFAULT_PROMPT_TEMPLATE: str | None = "a photo of {q}"  # measured; see docs
GENDER_WORDS = {
    "women": {"noi", "nok", "noknek", "women", "womens", "woman", "ladies", "holgy"},
    "men": {"ferfi", "ferfiak", "ferfiaknak", "men", "mens", "man"},
    "kids": {"gyerek", "gyermek", "lany", "fiu", "kids", "girls", "boys", "baby", "baba"},
}


def templated(english: str, template: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """The texts to embed; their embeddings are averaged.

    ``template`` holds ``{q}`` where the query goes, e.g. ``"a photo of {q}"``.
    """
    if template is None:
        return [english]
    templates = [template] if isinstance(template, str) else list(template)
    if not templates or any("{q}" not in item for item in templates):
        raise ValueError("each prompt template must contain {q}")
    return [item.replace("{q}", english) for item in templates]


def embed(text_encoder, texts: list[str]) -> list[float]:
    vectors = [text_encoder.encode(text) for text in texts]
    if len(vectors) == 1:
        return vectors[0]
    mean = [sum(values) / len(vectors) for values in zip(*vectors)]
    norm = math.sqrt(sum(value * value for value in mean)) or 1.0
    return [value / norm for value in mean]


def _photo_scores(
    image_encoder,
    query: list[float],
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
    matrix,
) -> tuple[list[dict[str, Any]], list[float]]:
    """Distinct offers with their best photo's cosine similarity."""
    entries = [entry for entry, _ in candidates]
    similarities: list[float]
    if matrix is not None and entries and all("_row" in entry for entry in entries):
        import numpy as np

        rows = np.fromiter((entry["_row"] for entry in entries), dtype=np.intp, count=len(entries))
        similarities = (matrix @ np.asarray(query, dtype=np.float32))[rows].tolist()
    else:
        similarities = [1.0 - image_encoder.distance(query, entry["features"]) for entry in entries]
    best: dict[int, tuple[float, dict[str, Any]]] = {}
    for similarity, (_, offer) in zip(similarities, candidates):
        current = best.get(offer["id"])
        if current is None or similarity > current[0]:
            best[offer["id"]] = (similarity, offer)
    order = sorted(best.values(), key=lambda item: (-item[0], item[1]["id"]))
    return [offer for _, offer in order], [score for score, _ in order]


def _standardise(values: list[float | None]) -> list[float]:
    present = [value for value in values if value is not None]
    if len(present) < 2:
        return [0.0 for _ in values]
    mean = sum(present) / len(present)
    spread = math.sqrt(sum((value - mean) ** 2 for value in present) / len(present)) or 1.0
    return [0.0 if value is None else (value - mean) / spread for value in values]


def lexical_terms(query: LookQuery) -> list[Term]:
    """One term per content word: as typed, its dictionary form and its English."""
    terms = []
    for word in query.words:
        if word.kind in {"filler", "connector"}:
            continue
        if word.kind == "gender" and word.gender:
            # "női", "women's" and "nőknek" all name the same shoppers.
            terms.append(tuple(sorted((form, True) for form in GENDER_WORDS[word.gender])))
            continue
        options = set()
        for term in query_terms(word.text):
            options.update(term)
        if word.lemma and word.language == "hu":
            options.add((word.lemma, len(word.lemma) < 4))
        for english in word.english.replace("-", " ").split() if word.kind != "unknown" else ():
            for term in query_terms(english):
                options.update(term)
        if options:
            terms.append(tuple(sorted(options)))
    return terms


def _gender_match(offer: dict[str, Any], gender: str) -> bool:
    return not GENDER_WORDS.get(gender, set()).isdisjoint(_words(offer.get("title") or ""))


@lru_cache(maxsize=65_536)
def _blob(text: str) -> str:
    return " ".join(_words(text))


@lru_cache(maxsize=65_536)
def _offer_blob(title: str, category: str | None, colours: tuple[str, ...]) -> str:
    """Folded searchable words of a public offer (see ``text_match.SEARCH_FIELDS``)."""
    return _blob(row_text({"title": title, "category": category, "colour_names": colours}))


def _lexical_scores(terms: list[Term], offers: list[dict[str, Any]]) -> dict[int, float]:
    """Word relevance for the offers that contain the most query words.

    Usually that means every word. When no offer has them all (a typo, or a word
    no title uses), the offers with the most of them stand in, so one unmatched
    word does not switch word relevance off. Among them, title hits and exact
    words rank first (``text_match.relevance``).
    """
    # Every accepted match contains the option as a substring, so a substring count
    # bounds the exact count from above; exact scoring stops below the best tier.
    needles = [{text for text, _ in term} for term in terms]
    bounds = []
    for offer in offers:
        blob = _offer_blob(
            offer.get("title") or "", offer.get("category"), tuple(offer.get("colour_names") or ())
        )
        bound = sum(any(needle in blob for needle in options) for options in needles)
        if bound:
            bounds.append((bound, offer))
    bounds.sort(key=lambda item: -item[0])
    best = 0
    found: dict[int, float] = {}
    for bound, offer in bounds:
        if bound < best:
            break
        per_term = [relevance([term], offer) for term in terms]
        count = sum(value > 0 for value in per_term)
        if count > best:
            best, found = count, {}
        if count == best and count:
            found[offer["id"]] = sum(per_term)
    return found


def _colour_terms(query: LookQuery) -> list[list[Term]]:
    return [query_terms(word) for word in query.colour_words]


def _colour_match(colour_terms: list[list[Term]], offer: dict[str, Any]) -> bool:
    """The offer's colour names, or its title, name one of the query's colours."""
    texts = [*(offer.get("colour_names") or []), offer.get("title") or ""]
    needles = {option for terms in colour_terms for term in terms for option, _ in term}
    if not any(needle in _blob(text) for text in texts for needle in needles):
        return False
    return any(matches_terms(terms, text) for terms in colour_terms for text in texts)


def hint_score(query: LookQuery, offer: dict[str, Any], colour_terms=None) -> float:
    """Share of the query's hints that this offer satisfies (0 without hints)."""
    checks = []
    if query.category:
        checks.append(offer.get("category") == query.category)
    if query.colour_words:
        terms = colour_terms if colour_terms is not None else _colour_terms(query)
        checks.append(_colour_match(terms, offer))
    if query.gender:
        checks.append(_gender_match(offer, query.gender))
    if query.size:
        checks.append(matches_size(query.size, offer.get("size_options") or []))
    return sum(checks) / len(checks) if checks else 0.0


def _filter_by_hints(query: LookQuery, offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep offers in the hinted category and colour, unless that would leave none."""
    kept = offers
    if query.category:
        kept = [offer for offer in kept if offer.get("category") == query.category] or kept
    if query.colour_words:
        terms = _colour_terms(query)
        kept = [offer for offer in kept if _colour_match(terms, offer)] or kept
    return kept


def rank(
    query: LookQuery,
    query_vector: list[float],
    image_encoder,
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
    matrix=None,
    *,
    product_text: ProductText | None = None,
    product_text_weight: float = DEFAULT_PRODUCT_TEXT_WEIGHT,
    blend: bool = True,
    fusion: str = "rrf",
    rrf_k: float = 60.0,
    lexical_weight: float = 1.0,
    hints: str = "boost",
    hint_weight: float = DEFAULT_HINT_WEIGHT,
) -> list[dict[str, Any]]:
    """Order eligible offers for the query, best first."""
    if fusion not in FUSIONS:
        raise ValueError(f"fusion must be one of {', '.join(FUSIONS)}")
    if hints not in HINT_MODES:
        raise ValueError(f"hints must be one of {', '.join(HINT_MODES)}")
    if not 0 <= product_text_weight <= 1:
        raise ValueError("product_text_weight must be between 0 and 1")
    if rrf_k <= 0 or lexical_weight < 0 or hint_weight < 0:
        raise ValueError("fusion weights must be nonnegative and rrf_k positive")
    offers, photo = _photo_scores(image_encoder, query_vector, candidates, matrix)
    if product_text is not None and product_text_weight > 0 and offers:
        text = product_text.similarities(query_vector, offers)
        visual = [
            (1 - product_text_weight) * a + product_text_weight * b
            for a, b in zip(_standardise(photo), _standardise(text))
        ]
    else:
        visual = _standardise(photo) if len(photo) > 1 else [0.0 for _ in photo]
    position = {offer["id"]: index for index, offer in enumerate(offers)}
    score = {offer["id"]: value for offer, value in zip(offers, visual)}
    if blend and hints == "boost":
        # A satisfied hint moves an offer up the visual order by `hint_weight`
        # standard deviations; it is not a separate list, so a visually close
        # offer is not counted twice.
        # Only offers that the bonus could lift into the first HINT_DEPTH places are
        # scored; the order of the first HINT_DEPTH is exact either way.
        colour_terms = _colour_terms(query)
        values = sorted(score.values(), reverse=True)
        floor = values[min(HINT_DEPTH, len(values)) - 1] - hint_weight if values else 0.0
        for offer in offers:
            if score[offer["id"]] >= floor:
                score[offer["id"]] += hint_weight * hint_score(query, offer, colour_terms)
    ordered = sorted(offers, key=lambda offer: (-score[offer["id"]], position[offer["id"]]))
    if blend and hints == "filter":
        ordered = _filter_by_hints(query, ordered)
    if not blend:
        return ordered
    terms = lexical_terms(query)
    lexical = _lexical_scores(terms, ordered) if terms else {}
    if fusion == "weighted":
        top = max(lexical.values(), default=0.0) or 1.0
        fused = {
            offer["id"]: score[offer["id"]] + lexical_weight * lexical.get(offer["id"], 0.0) / top
            for offer in ordered
        }
    else:
        fused = {offer["id"]: 1 / (rrf_k + rank_) for rank_, offer in enumerate(ordered, 1)}
        listed = sorted(
            (offer for offer in ordered if offer["id"] in lexical),
            key=lambda offer: -lexical[offer["id"]],
        )  # stable: ties keep the visual order
        for rank_, offer in enumerate(listed, 1):
            fused[offer["id"]] += lexical_weight / (rrf_k + rank_)
    return sorted(ordered, key=lambda offer: -fused[offer["id"]])  # stable on ties

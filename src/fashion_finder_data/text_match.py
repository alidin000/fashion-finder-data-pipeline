"""Accent-insensitive Hungarian/English matching and relevance for shopper search.

Retailer titles and colours are mostly Hungarian; shoppers often type English or
leave out accents. Each query word may match itself or a translation, compared
word by word rather than as a raw substring, so "red" no longer hits "Redőzött".
"""

from __future__ import annotations

import re

from .taxonomy import fold

TRANSLATIONS = {
    # Clothing words, for offers whose category is not mapped yet.
    "jacket": ("dzseki",),
    "coat": ("kabat",),
    "dress": ("ruha",),
    "sweater": ("pulover",),
    "jumper": ("pulover",),
    "jeans": ("farmernadrag",),  # "farmer" alone also names denim jackets
    "denim": ("farmer",),
    "trousers": ("nadrag",),
    "pants": ("nadrag",),
    "skirt": ("szoknya",),
    "shorts": ("rovidnadrag",),
    "shoe": ("cipo",),
    "bag": ("taska",),
    "blouse": ("bluz",),
    # Colours.
    "black": ("fekete",),
    "white": ("feher",),
    "red": ("piros", "voros"),
    "blue": ("kek",),
    "navy": ("sotetkek",),
    "green": ("zold",),
    "yellow": ("sarga",),
    "brown": ("barna",),
    "grey": ("szurke",),
    "gray": ("szurke",),
    "beige": ("bezs",),
    "pink": ("rozsaszin",),
    "purple": ("lila",),
    "orange": ("narancs",),
    "cream": ("krem",),
    "burgundy": ("bordo",),
}
# Hungarian → English, for Hungarian queries against English titles (Zara /hu/en/)
# and for text-to-photo search, whose model only understands English.
REVERSE: dict[str, str] = {}
for _english, _hungarian in TRANSLATIONS.items():
    for _word in _hungarian:
        REVERSE.setdefault(_word, _english)

SEARCH_FIELDS = ("title", "brand", "description", "category", "source_category", "colour_names")
FIELD_WEIGHTS = {
    "title": 3.0,
    "category": 2.0,
    "source_category": 2.0,
    "colour_names": 1.5,
    "brand": 1.0,
    "description": 0.5,
}
_WORD = re.compile(r"\w+")

# An option is (text, whole): `whole` options must equal a word (English plurals
# allowed); the others may also start or end a word, which covers Hungarian
# endings ("kabátok") and compound heads ("gyapjúkabát").
Option = tuple[str, bool]
Term = tuple[Option, ...]


def row_text(row: dict) -> str:
    return " ".join(str(row.get(field) or "") for field in SEARCH_FIELDS)


def _words(text: str) -> list[str]:
    return _WORD.findall(fold(text))


def _hungarian_stems(word: str) -> tuple[str, ...]:
    return (
        word,
        *(word.removesuffix(end) for end in ("ok", "ek", "ak", "k") if word.endswith(end)),
    )


def query_terms(query: str | None) -> list[Term]:
    """Return, for each query word, the options that can satisfy it."""
    terms = []
    for word in _words(query or ""):
        # Short words such as "red" or "bag" must be whole words.
        options: set[Option] = {(word, len(word) < 4)}
        for stem in (word, word.removesuffix("es"), word.removesuffix("s")):
            options.update((hungarian, False) for hungarian in TRANSLATIONS.get(stem, ()))
        for stem in _hungarian_stems(word):
            if stem in REVERSE:
                options.add((REVERSE[stem], True))
        terms.append(tuple(sorted(options)))
    return terms


def _quality(options: Term, words: list[str]) -> float:
    """1.0 for an exact word, 0.6 for a partial word, 0.0 for no match."""
    best = 0.0
    for text, whole in options:
        for word in words:
            if word in (text, text + "s", text + "es"):
                return 1.0
            if not whole and (
                word.startswith(text) or word.endswith(text) or (len(text) >= 5 and text in word)
            ):
                best = 0.6
    return best


def matches_terms(terms: list[Term], text: str) -> bool:
    """Every query word must appear, in some accepted form, somewhere in ``text``."""
    words = _words(text)
    return all(_quality(options, words) > 0 for options in terms)


def relevance(terms: list[Term], row: dict) -> float:
    """Score how well a row matches: title hits count most, exact words beat partial ones."""
    fields = {field: _words(str(row.get(field) or "")) for field in FIELD_WEIGHTS}
    return sum(
        max(weight * _quality(options, fields[field]) for field, weight in FIELD_WEIGHTS.items())
        for options in terms
    )


def english_query(text: str) -> str:
    """Translate known Hungarian clothing and colour words for an English-only model."""
    translated = []
    for word in _words(text):
        english = next((REVERSE[stem] for stem in _hungarian_stems(word) if stem in REVERSE), None)
        translated.append(english or word)
    return " ".join(translated)


def matches_colour(wanted: str, colours: list[str]) -> bool:
    terms = query_terms(wanted)
    return any(matches_terms(terms, colour) for colour in colours)


def matches_size(wanted: str, sizes: list[str]) -> bool:
    """``M`` matches a combined size such as ``M/L``."""
    target = fold(wanted).strip()
    return any(
        target == fold(size).strip() or target in (part.strip() for part in fold(size).split("/"))
        for size in sizes
    )

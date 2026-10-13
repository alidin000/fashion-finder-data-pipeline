"""Accent-insensitive Hungarian/English matching for shopper search.

Retailer titles and colours are Hungarian; shoppers often type English or
leave out accents. Each query word may match itself or a Hungarian equivalent.
"""

from __future__ import annotations

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


SEARCH_FIELDS = ("title", "brand", "description", "category", "source_category", "colour_names")


def row_text(row: dict) -> str:
    return " ".join(str(row.get(field) or "") for field in SEARCH_FIELDS)


def query_terms(query: str | None) -> list[tuple[str, ...]]:
    """Return, for each query word, the folded terms that can satisfy it."""
    terms = []
    for word in fold(query or "").split():
        options = {word}
        for stem in (word, word.removesuffix("es"), word.removesuffix("s")):
            options.update(TRANSLATIONS.get(stem, ()))
        terms.append(tuple(sorted(options)))
    return terms


def matches_terms(terms: list[tuple[str, ...]], text: str) -> bool:
    """Every query word must appear, in any accepted form, somewhere in ``text``."""
    folded = fold(text)
    return all(any(option in folded for option in options) for options in terms)


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

"""Small shared clothing taxonomy for the currently supported market."""

from __future__ import annotations

import re
import unicodedata

CATEGORIES = {
    "coats",
    "jackets",
    "dresses",
    "sweaters",
    "shirts",
    "blouses",
    "tops",
    "trousers",
    "jeans",
    "skirts",
    "shorts",
    "shoes",
    "bags",
    "accessories",
}

ALIASES = {
    "kabát": "coats",
    "kabátok": "coats",
    "dzseki": "jackets",
    "dzsekik": "jackets",
    "ruha": "dresses",
    "ruhák": "dresses",
    "pulóver": "sweaters",
    "pulóverek": "sweaters",
    "ing": "shirts",
    "ingek": "shirts",
    "blúz": "blouses",
    "blúzok": "blouses",
    "felső": "tops",
    "felsők": "tops",
    "nadrág": "trousers",
    "nadrágok": "trousers",
    "farmer": "jeans",
    "farmerek": "jeans",
    "szoknya": "skirts",
    "szoknyák": "skirts",
    "rövidnadrág": "shorts",
    "rövidnadrágok": "shorts",
    "cipő": "shoes",
    "cipők": "shoes",
    "táska": "bags",
    "táskák": "bags",
    "kiegészítő": "accessories",
    "kiegészítők": "accessories",
}


def normalize_category(value: str | None) -> str | None:
    """Map known English/Hungarian labels; leave unknowns unclassified."""
    if not value:
        return None
    key = re.sub(r"[\s_-]+", " ", value.strip().casefold())
    return key if key in CATEGORIES else ALIASES.get(key)


def fold(value: str) -> str:
    """Casefold and drop accents, so ``Kabát`` and ``kabat`` compare equal."""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


SLUG_ALIASES = {fold(alias): category for alias, category in ALIASES.items()}


def category_from_slug(slug: str) -> str | None:
    """Map a URL slug such as ``dzsekik`` only when its words name one category.

    ``kabatok-dzsekik`` names two categories and stays unclassified.
    """
    matches = {SLUG_ALIASES.get(word) for word in fold(slug).split("-")} - {None}
    return matches.pop() if len(matches) == 1 else None

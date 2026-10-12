"""Small shared clothing taxonomy for the currently supported market."""

from __future__ import annotations

import re

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

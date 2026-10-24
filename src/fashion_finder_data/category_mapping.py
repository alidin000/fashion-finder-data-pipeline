"""Map source category evidence to the shared taxonomy keys.

The shared keys and base aliases live in ``taxonomy``. This module adds the labels
collection meets in practice: breadcrumb trails, Google product category paths,
English shop labels and LPP URL slugs. A label that names two categories, or none,
stays unclassified; nothing here guesses.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from .taxonomy import CATEGORIES, SLUG_ALIASES, fold, normalize_category

# Whole labels or slugs (accent-folded) that the base aliases do not cover.
EXTRA_LABELS = {
    # Hungarian shop labels
    "polo": "tops",
    "polok": "tops",
    "galleros polo": "tops",
    "galleros polok": "tops",
    "top": "tops",
    "topok": "tops",
    "polok es topok": "tops",
    "polok topok": "tops",
    "sort": "shorts",
    "sortok": "shorts",
    "melegitofelso": "sweaters",
    "melegitofelsok": "sweaters",
    "melegito felso": "sweaters",
    "melegito felsok": "sweaters",
    "jogging felso": "sweaters",
    "jogging topok": "sweaters",
    "kardigan": "sweaters",
    "kardiganok": "sweaters",
    "puloverek kardiganok": "sweaters",
    "pulcsi": "sweaters",
    "farmernadrag": "jeans",
    "farmernadragok": "jeans",
    "csizma": "shoes",
    "csizmak": "shoes",
    "sportcipo": "shoes",
    "sportcipok": "shoes",
    "bakancs": "shoes",
    "bakancsok": "shoes",
    # English shop and feed labels
    "coat": "coats",
    "jacket": "jackets",
    "dress": "dresses",
    "sweater": "sweaters",
    "jumper": "sweaters",
    "jumpers": "sweaters",
    "cardigan": "sweaters",
    "cardigans": "sweaters",
    "hoodie": "sweaters",
    "hoodies": "sweaters",
    "sweatshirt": "sweaters",
    "sweatshirts": "sweaters",
    "shirt": "shirts",
    "blouse": "blouses",
    "t shirt": "tops",
    "t shirts": "tops",
    "tshirt": "tops",
    "tshirts": "tops",
    "tee": "tops",
    "tees": "tops",
    "pants": "trousers",
    "trouser": "trousers",
    "skirt": "skirts",
    "short": "shorts",
    "shoe": "shoes",
    "sneakers": "shoes",
    "boots": "shoes",
    "bag": "bags",
    "handbag": "bags",
    "handbags": "bags",
    # Sinsay's English children's slugs (k-*)
    "k jogging tops": "sweaters",
    "k trousers": "trousers",
    "k jeans": "jeans",
    "k tshirts": "tops",
    "k dresses": "dresses",
}
_WORD_LABELS = {
    **{fold(alias): category for alias, category in EXTRA_LABELS.items() if " " not in alias},
    **{category: category for category in CATEGORIES},
}


def _key(value: str) -> str:
    return re.sub(r"[\s_-]+", " ", fold(value.strip())).strip()


def category_from_label(label: str | None) -> str | None:
    """Map one whole label (``Kabátok``, ``T-shirts``, ``Pólók``) or return None."""
    if not label or not label.strip():
        return None
    return normalize_category(label) or EXTRA_LABELS.get(_key(label))


def category_from_slug_extended(
    slug: str, overrides: Mapping[str, str] | None = None
) -> str | None:
    """Slug rules: a per-source override, then a whole known label, then its words.

    ``dzsekik-kabatok`` names two categories and stays unclassified, as does a slug
    whose words map to different categories under the base or the extra aliases.
    """
    if overrides and slug in overrides:
        return overrides[slug] or None
    whole = category_from_label(slug.replace("-", " "))
    if whole:
        return whole
    words = fold(slug).split("-")
    found = {SLUG_ALIASES.get(word) or _WORD_LABELS.get(word) for word in words} - {None}
    return found.pop() if len(found) == 1 else None


_JOINER = re.compile(r"\s*(?:&|\+|,|\band\b|\bés\b|\bes\b)\s*", re.IGNORECASE)
AMBIGUOUS = "ambiguous"


def _segment(label: str) -> str | None:
    """Category of one segment, ``AMBIGUOUS`` if it names two, else None.

    ``Pólók & topok`` is tops; ``Coats & Jackets`` is ambiguous; ``Apparel &
    Accessories`` is not a category because ``Apparel`` maps to nothing.
    """
    whole = category_from_label(label)
    if whole:
        return whole
    parts = [part for part in _JOINER.split(label) if part.strip()]
    if len(parts) < 2:
        return None
    mapped = [category_from_label(part) for part in parts]
    if None in mapped:
        return AMBIGUOUS if len(set(mapped) - {None}) > 1 else None
    return mapped[0] if len(set(mapped)) == 1 else AMBIGUOUS


def category_from_path(path: str) -> str | None:
    """Most specific mappable segment of a ``>``- or ``/``-separated category path.

    Works for breadcrumb text and Google product category names such as
    ``Apparel & Accessories > Clothing > Dresses``. A segment that names two kinds
    of garment, such as ``Coats & Jackets``, stops the search unclassified.
    """
    segments = [part.strip() for part in re.split(r"\s*[>/|›»]\s*", path) if part.strip()]
    for segment in reversed(segments):
        mapped = _segment(segment)
        if mapped == AMBIGUOUS:
            return None
        if mapped:
            return mapped
    return None


def map_category(
    *,
    breadcrumbs: Iterable[str] = (),
    google_category: str | None = None,
    product_category: str | None = None,
    listing_category: str | None = None,
    product_name: str | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(source_label, category_key)`` from the most specific evidence.

    Order: the product's own breadcrumb trail, its schema.org ``category`` text, its
    Google product category, then the category of the listing it came from. A
    breadcrumb equal to the product name is skipped.
    """
    name = (product_name or "").strip().casefold()
    crumbs = [
        crumb.strip()
        for crumb in breadcrumbs
        if isinstance(crumb, str) and crumb.strip() and crumb.strip().casefold() != name
    ]
    for crumb in reversed(crumbs):
        mapped = _segment(crumb)
        if mapped == AMBIGUOUS:
            break
        if mapped:
            return crumb, mapped
    for candidate in (product_category, google_category):
        if isinstance(candidate, str) and candidate.strip() and not candidate.strip().isdigit():
            mapped = category_from_path(candidate)
            if mapped:
                return candidate.strip(), mapped
    if listing_category:
        return listing_category, category_from_label(listing_category)
    label = crumbs[-1] if crumbs else product_category or google_category
    return (label.strip() if isinstance(label, str) and label.strip() else None), None

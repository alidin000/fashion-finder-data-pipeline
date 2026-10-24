"""Choose the product photos worth storing and fetching.

Shops mix packshots with size charts, brand logos, colour swatches and banners.
Those are detectable from the URL or the image metadata a page publishes, and
they hurt photo search. This module drops them and puts packshots first; it
never invents a photo.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit

# Words in a URL path or caption that mark a non-product image.
NON_PRODUCT = re.compile(
    r"size[-_ ]?(?:chart|guide|table)|sizechart|measure|m[eé]rett[aá]bl[aá]zat|"
    r"(?:^|[/_.-])logos?(?:[/_.-]|$)|brand[-_]?logo|favicon|sprite|(?:^|[/_.-])icons?(?:[/_.-]|$)|"
    r"placeholder|no[-_]?image|noimage|blank\.|spacer|banner|badge|swatch|"
    r"(?:^|[/_.-])pixel(?:[/_.-]|$)|tracking|payment|flag[-_]",
    re.IGNORECASE,
)
# Hints that an image is a clean product shot.
PACKSHOT = re.compile(
    r"packshot|(?:^|[/_.-])(?:pk|front|main|primary)(?:[/_.-]|$)",
    re.IGNORECASE,
)
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".avif")


def image_url(item: Any) -> str | None:
    if isinstance(item, dict):
        item = item.get("url") or item.get("contentUrl") or item.get("src")
    return item.strip() if isinstance(item, str) and item.strip() else None


def _caption(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return " ".join(
        str(item.get(key) or "") for key in ("caption", "name", "alt", "description", "role")
    )


def is_non_product_image(url: str, caption: str = "") -> bool:
    """True for size charts, logos, swatches, placeholders, tracking pixels or vector art."""
    path = unquote(urlsplit(url).path).lower()
    if path.endswith((".svg", ".gif", ".ico")):
        return True
    return bool(NON_PRODUCT.search(path) or (caption and NON_PRODUCT.search(caption)))


def rank_images(images: Any) -> list[str]:
    """Drop non-product images and put likely packshots first, keeping source order otherwise.

    Accepts URLs or ``ImageObject``-like dictionaries with ``url``/``contentUrl`` and a
    caption. The result is de-duplicated.
    """
    values = images if isinstance(images, list) else [images] if images else []
    kept: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for position, item in enumerate(values):
        url = image_url(item)
        if url is None or url in seen:
            continue
        seen.add(url)
        caption = _caption(item)
        if is_non_product_image(url, caption):
            continue
        packshot = bool(PACKSHOT.search(urlsplit(url).path) or PACKSHOT.search(caption))
        kept.append((0 if packshot else 1, position, url))
    return [url for _, _, url in sorted(kept)]


def select_photos(urls: list[str], count: int) -> list[str]:
    """The best ``count`` stored photo URLs of one offer for fetching or indexing."""
    return rank_images([url for url in urls if isinstance(url, str)])[:count]

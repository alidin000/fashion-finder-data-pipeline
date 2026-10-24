"""Find the garment in a shopper's photo, and name its category and colour.

Marqo-FashionSigLIP embeds photos and English text in one space. Regions of the
photo are embedded in one batched model run and compared with cached prompts such
as "a photo of a jacket"; the region that looks most like one garment becomes the
query, and other strong garments are offered as alternatives. The same prompts
classify the query's category and colour (zero-shot: no training on our labels).

All of this needs the FashionSigLIP text tower. With the colour-layout descriptor,
the ImageNet ViT, or no text model configured, :func:`understand_photo` embeds the
whole photo and reports no crop, category or colour.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from PIL import Image, ImageOps

from .visual_encoder import FASHION_SIGLIP_SPACE

# Each category is a few English phrases (article, noun); its score is the best phrase.
CATEGORY_PHRASES: dict[str, tuple[tuple[str, str], ...]] = {
    "coats": (("a", "coat"), ("a", "long coat")),
    "jackets": (("a", "jacket"), ("a", "short jacket")),
    "dresses": (("a", "dress"),),
    "sweaters": (("a", "sweater"), ("a", "knitted jumper")),
    "shirts": (("a", "shirt"), ("a", "button-down shirt")),
    "blouses": (("a", "blouse"),),
    "tops": (("a", "top"), ("a", "t-shirt")),
    "trousers": (("", "trousers"), ("", "pants")),
    "jeans": (("", "jeans"),),
    "skirts": (("a", "skirt"),),
    "shorts": (("", "shorts"),),
    "shoes": (("", "shoes"), ("", "sneakers"), ("", "boots")),
    "bags": (("a", "bag"), ("a", "handbag")),
    "accessories": (("a", "hat"), ("a", "scarf"), ("a", "belt"), ("", "sunglasses")),
}
TEMPLATES = ("a photo of {}", "a product photo of {}")
# The repository's colour vocabulary: every word is a TRANSLATIONS key, so a predicted
# colour also matches Hungarian colour names such as "fekete" in offers.
COLOURS = (
    "black",
    "white",
    "grey",
    "beige",
    "cream",
    "brown",
    "red",
    "burgundy",
    "pink",
    "orange",
    "yellow",
    "green",
    "blue",
    "navy",
    "purple",
)

# Measured on the practice catalogue (packshots) and on licence-safe Fashionpedia
# street photos with INT8 Marqo-FashionSigLIP; see docs/local-image-index.md.
# Softmax temperature for prompt similarities; SigLIP similarities are small, so a
# large scale turns their differences into usable probabilities.
ZERO_SHOT_SCALE = 100.0
# The region to search is the one that looks most like the photo's main garment and
# least like "a photo of a person" (faces and skin pull matches towards model shots).
PERSON_PROMPT = "a photo of a person"
PERSON_PENALTY = 2.0
# ...and it replaces the whole photo only when it beats the whole photo by this
# margin, so product photos stay whole (3 % of GLAMI packshots were cropped).
CROP_MARGIN = 0.015
# A photo whose whole-frame embedding is already this close to some indexed photo
# is a product photo: it is searched whole without trying regions (one model run
# instead of eight; same accuracy on the practice catalogue).
FOCUS_SKIP_SIMILARITY = 0.85
# Other garments are offered only when their region reads clearly as one category.
ALTERNATIVE_CONFIDENCE = 0.8
MAX_ALTERNATIVES = 3
MAX_OVERLAP = 0.35  # intersection over union; more overlap means the same garment
# Report a category or colour only at this probability or above: 86 % (category) and
# 79 % (colour) of those reports were right on the practice catalogue.
CATEGORY_CONFIDENCE = 0.6
COLOUR_CONFIDENCE = 0.7


@dataclass(frozen=True)
class Box:
    """A region as fractions (0–1) of the upright photo."""

    x: float
    y: float
    width: float
    height: float

    @property
    def area(self) -> float:
        return self.width * self.height

    def overlap(self, other: Box) -> float:
        """Intersection over union."""
        width = min(self.x + self.width, other.x + other.width) - max(self.x, other.x)
        height = min(self.y + self.height, other.y + other.height) - max(self.y, other.y)
        shared = max(0.0, width) * max(0.0, height)
        return shared / (self.area + other.area - shared) if shared else 0.0

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        """A non-empty (left, top, right, bottom) pixel box inside the photo."""
        left = min(width - 1, max(0, math.floor(self.x * width)))
        top = min(height - 1, max(0, math.floor(self.y * height)))
        right = max(left + 1, min(width, round((self.x + self.width) * width)))
        bottom = max(top + 1, min(height, round((self.y + self.height) * height)))
        return left, top, right, bottom

    def as_json(self) -> dict[str, float]:
        return {
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "width": round(self.width, 4),
            "height": round(self.height, 4),
        }


FULL = Box(0.0, 0.0, 1.0, 1.0)
# Upright and square photos: a standing person's torso, legs and feet sit in bands.
TALL_REGIONS = (
    FULL,
    Box(0.0, 0.0, 1.0, 0.5),  # top half
    Box(0.0, 0.5, 1.0, 0.5),  # bottom half
    Box(0.2, 0.2, 0.6, 0.6),  # centre
    Box(0.0, 0.1, 1.0, 0.45),  # torso band
    Box(0.0, 0.4, 1.0, 0.45),  # legs band
    Box(0.0, 0.7, 1.0, 0.3),  # feet band
    Box(0.15, 0.05, 0.7, 0.9),  # centre column, trimming the sides
)
# Wide photos: the garment or person is usually one column of the frame.
WIDE_REGIONS = (
    FULL,
    Box(0.0, 0.0, 0.5, 1.0),  # left half
    Box(0.5, 0.0, 0.5, 1.0),  # right half
    Box(0.2, 0.2, 0.6, 0.6),  # centre
    Box(0.25, 0.0, 0.5, 1.0),  # centre column
    Box(0.25, 0.0, 0.5, 0.55),  # upper centre
    Box(0.25, 0.45, 0.5, 0.55),  # lower centre
    Box(0.0, 0.0, 1.0, 0.5),  # top half
)


def candidate_regions(width: int, height: int) -> tuple[Box, ...]:
    """Eight regions to try, the whole photo first, chosen by the photo's shape."""
    return WIDE_REGIONS if width > 1.15 * height else TALL_REGIONS


def _with_article(article: str, words: str) -> str:
    if article == "a" and words[:1] in "aeiou":
        return f"an {words}"
    return f"{article} {words}".strip()


def _prompt_matrix(np, text_encoder: Any, phrases: list[str]):
    """One unit row per phrase: the normalised mean of its template embeddings."""
    rows = []
    for phrase in phrases:
        mean = np.mean(
            [text_encoder.encode(template.format(phrase)) for template in TEMPLATES], axis=0
        )
        rows.append(mean / np.linalg.norm(mean))
    return np.asarray(rows, dtype=np.float64)


@lru_cache(maxsize=2)
def _category_bank(text_encoder: Any) -> tuple[Any, tuple[str, ...], Any]:
    """Prompt rows for every category phrase, the category of each row, and the
    person prompt. Cached per loaded text model, so prompts are embedded once.
    """
    import numpy as np

    labels = tuple(
        category for category in sorted(CATEGORY_PHRASES) for _ in CATEGORY_PHRASES[category]
    )
    phrases = [
        _with_article(article, noun)
        for category in sorted(CATEGORY_PHRASES)
        for article, noun in CATEGORY_PHRASES[category]
    ]
    person = np.asarray(text_encoder.encode(PERSON_PROMPT), dtype=np.float64)
    return _prompt_matrix(np, text_encoder, phrases), labels, person / np.linalg.norm(person)


@lru_cache(maxsize=32)
def _colour_bank(text_encoder: Any, category: str | None) -> Any:
    """Rows for "a photo of a black jacket" and so on, in ``COLOURS`` order."""
    import numpy as np

    article, noun = CATEGORY_PHRASES[category][0] if category else ("", "clothing")
    phrases = [_with_article(article, f"{colour} {noun}") for colour in COLOURS]
    return _prompt_matrix(np, text_encoder, phrases)


def _category_scores(np, similarities, labels: tuple[str, ...]):
    """Per row, the best phrase similarity of each category, in sorted category order."""
    categories = sorted(CATEGORY_PHRASES)
    columns = [
        similarities[:, [i for i, label in enumerate(labels) if label == category]].max(axis=1)
        for category in categories
    ]
    return np.stack(columns, axis=1), categories


def _softmax(np, values):
    scaled = ZERO_SHOT_SCALE * (values - values.max())
    weights = np.exp(scaled)
    return weights / weights.sum()


@dataclass
class PhotoUnderstanding:
    """The embedding to search with and what the system understood about the photo."""

    vector: list[float]
    crop_source: str = "none"  # "auto", "manual" or "none"
    crop: Box | None = None
    category_probabilities: dict[str, float] | None = None
    colour: tuple[str, float] | None = None
    alternatives: list[tuple[str, Box]] = field(default_factory=list)

    @property
    def category(self) -> tuple[str, float] | None:
        if not self.category_probabilities:
            return None
        return max(self.category_probabilities.items(), key=lambda item: item[1])

    def as_json(self) -> dict[str, Any]:
        """The response's ``query`` block; category and colour only when confident."""
        category, colour = self.category, self.colour
        return {
            "crop_source": self.crop_source,
            "crop": self.crop.as_json() if self.crop else None,
            "category": {"value": category[0], "confidence": round(category[1], 2)}
            if category and category[1] >= CATEGORY_CONFIDENCE
            else None,
            "colour": {"value": colour[0], "confidence": round(colour[1], 2)}
            if colour and colour[1] >= COLOUR_CONFIDENCE
            else None,
            "alternatives": [
                {"category": name, "crop": box.as_json()} for name, box in self.alternatives
            ],
        }


def can_understand(image_encoder: Any, text_encoder: Any | None) -> bool:
    """Auto-crop and zero-shot need FashionSigLIP's paired vision and text towers."""
    return (
        text_encoder is not None
        and getattr(image_encoder, "space", None) == FASHION_SIGLIP_SPACE
        and getattr(text_encoder, "space", None) == FASHION_SIGLIP_SPACE
        and hasattr(image_encoder, "embed")
        and hasattr(image_encoder, "query_vector")
    )


def _choose_region(regions, scores, person, main: int) -> int:
    """Index of the region to search: the whole photo unless a region clearly wins.

    A region's value is its similarity to the photo's main category minus
    ``PERSON_PENALTY`` times its similarity to a person.
    """
    values = [scores[i][main] - PERSON_PENALTY * person[i] for i in range(len(regions))]
    best = max(range(1, len(regions)), key=lambda i: (values[i], regions[i].area))
    return best if values[best] > values[0] + CROP_MARGIN else 0


def _alternatives(np, regions, scores, categories, chosen: int) -> list[tuple[str, Box]]:
    """Other garments in the photo: confident, distinct categories that overlap little.

    The overlap test includes the searched area, which is the whole photo when nothing
    was cropped; then only small regions such as the feet band qualify. That keeps
    false alternatives on single-product photos rare (12 % of GLAMI packshots).
    """
    chosen_category = categories[int(scores[chosen].argmax())]
    picked: list[tuple[str, Box]] = []
    for index in sorted(range(1, len(regions)), key=lambda i: -scores[i].max()):
        box = regions[index]
        probabilities = _softmax(np, scores[index])
        name = categories[int(probabilities.argmax())]
        if (
            index == chosen
            or probabilities.max() < ALTERNATIVE_CONFIDENCE
            or name == chosen_category
            or box.overlap(regions[chosen]) > MAX_OVERLAP
            or any(name == other or box.overlap(seen) > MAX_OVERLAP for other, seen in picked)
        ):
            continue
        picked.append((name, box))
        if len(picked) == MAX_ALTERNATIVES:
            break
    return picked


def understand_photo(
    image_encoder: Any,
    text_encoder: Any | None,
    image: Image.Image,
    *,
    auto_crop: bool = True,
    zero_shot: bool = True,
    manual_crop: bool = False,
    nearest: Callable[[Any], float] | None = None,
) -> PhotoUnderstanding:
    """Embed the photo for search, focusing on one garment when ``auto_crop`` allows.

    ``manual_crop`` means the shopper already cropped the photo, so auto-crop is off.
    ``nearest`` gives the best cosine similarity of an embedding to the index; a
    photo at least ``FOCUS_SKIP_SIMILARITY`` close is searched whole. Without a
    FashionSigLIP text model, auto-crop and zero-shot are off and the whole photo is
    embedded exactly as before.
    """
    source = "manual" if manual_crop else "none"
    if not can_understand(image_encoder, text_encoder) or not (auto_crop or zero_shot):
        return PhotoUnderstanding(vector=image_encoder.encode(image), crop_source=source)
    import numpy as np

    rgb = ImageOps.exif_transpose(image).convert("RGB")
    prompts, labels, person_prompt = _category_bank(text_encoder)
    embedded = image_encoder.embed([rgb])
    cropping = (
        auto_crop
        and not manual_crop
        and (nearest is None or nearest(embedded[0]) < FOCUS_SKIP_SIMILARITY)
    )
    regions = candidate_regions(*rgb.size) if cropping else (FULL,)
    photos = [rgb, *(rgb.crop(box.pixels(*rgb.size)) for box in regions[1:])]
    if cropping:
        embedded = np.concatenate([embedded, image_encoder.embed(photos[1:])])
    scores, categories = _category_scores(np, embedded @ prompts.T, labels)
    chosen = (
        _choose_region(regions, scores, embedded @ person_prompt, int(scores[0].argmax()))
        if cropping
        else 0
    )
    vector = image_encoder.query_vector(photos[chosen], embedded[chosen])
    result = PhotoUnderstanding(
        vector=vector.tolist(),
        crop_source="auto" if chosen else source,
        crop=regions[chosen] if chosen else None,
        alternatives=_alternatives(np, regions, scores, categories, chosen) if cropping else [],
    )
    if zero_shot:
        query_scores, _ = _category_scores(np, (prompts @ vector)[None, :], labels)
        probabilities = _softmax(np, query_scores[0])
        result.category_probabilities = {
            name: float(value) for name, value in zip(categories, probabilities, strict=True)
        }
        name, confidence = result.category
        colours = _softmax(
            np,
            _colour_bank(text_encoder, name if confidence >= CATEGORY_CONFIDENCE else None)
            @ vector,
        )
        result.colour = (COLOURS[int(colours.argmax())], float(colours.max()))
    return result

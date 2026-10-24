"""Map public dataset rows to the shared taxonomy, colours and a practice product record."""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..taxonomy import CATEGORIES, fold

# GLAMI-1M `category_name` → taxonomy category. Unlisted classes (swimwear, lingerie,
# nightwear, baby and home goods) are left out of the practice catalogue.
GLAMI_CATEGORIES = {
    **dict.fromkeys(("womens-coats", "mens-coats", "womens-parkas", "mens-parkas"), "coats"),
    **dict.fromkeys(
        (
            "womens-sport-jackets",
            "mens-sport-jackets",
            "womens-quilted-jackets",
            "mens-quilted-jacket",
            "womens-bombers",
            "mens-bombers",
            "womens-leather-jackets",
            "mens-leather-jackets",
            "womens-denim-jackets",
            "mens-denim-jackets",
            "womens-suit-jackets-and-blazers",
            "mens-suit-jackets-and-blazers",
            "womens-windbreakers",
            "mens-windbreakers",
        ),
        "jackets",
    ),
    **dict.fromkeys(("dresses", "girls-dresses"), "dresses"),
    **dict.fromkeys(
        (
            "womens-sweaters",
            "mens-sweaters",
            "girls-sweaters",
            "boys-sweaters",
            "womens-sweatshirts",
            "mens-sweatshirts",
            "girls-sweatshirts",
            "boys-sweatshirts",
        ),
        "sweaters",
    ),
    **dict.fromkeys(("mens-shirts", "boys-shirts"), "shirts"),
    "womens-blouses-and-shirts": "blouses",
    **dict.fromkeys(
        (
            "womens-tops-tank-tops-and-t-shirts",
            "mens-t-shirts-and-tank-tops",
            "boys-t-shirts",
            "girls-t-shirts-and-shirts",
        ),
        "tops",
    ),
    **dict.fromkeys(("womens-pants", "mens-pants", "boys-pants", "girls-pants"), "trousers"),
    **dict.fromkeys(("womens-skirts", "girls-skirts"), "skirts"),
    **dict.fromkeys(("women-s-shorts", "mens-short", "boys-shorts"), "shorts"),
    **dict.fromkeys(
        (
            "womens-sneakers",
            "mens-sneakers",
            "women-dress-shoes",
            "men-dress-shoes",
            "heels",
            "womens-boots-and-booties",
            "mens-boots",
            "womens-sandals",
            "mens-sandals",
            "womens-flats",
            "womens-loafers-and-moccasins",
            "mens-loafers-and-moccasins",
            "womens-espadrilles",
            "mens-espadrilles",
            "womens-outdoor-shoes",
            "mens-outdoor-shoes",
            "womens-mule",
        ),
        "shoes",
    ),
    **dict.fromkeys(
        (
            "handbags",
            "shoulder-bags",
            "mens-bags",
            "womens-backpacks",
            "mens-backpacks",
            "womens-fanny-packs",
            "womens-travel-bags",
        ),
        "bags",
    ),
    **dict.fromkeys(
        (
            "womens-necklaces",
            "womens-earrings",
            "womens-bracelets",
            "womens-rings",
            "mens-rings",
            "womens-sunglasses",
            "mens-sunglasses",
            "womens-belts",
            "mens-belts",
            "womens-headwear",
            "mens-headwear",
            "mens-ties",
            "mens-watches",
            "womens-watches",
            "mens-wallets",
            "womens-wallets",
            "womens-shawls-and-scarves",
            "mens-scarves-and-shawls",
        ),
        "accessories",
    ),
}
# ABO `product_type` → taxonomy category. ABO holds no garments, only these.
ABO_CATEGORIES = {
    **dict.fromkeys(("SHOES", "BOOT", "SANDAL", "TECHNICAL_SPORT_SHOE"), "shoes"),
    **dict.fromkeys(("HANDBAG", "TOTE_BAG", "BACKPACK", "BAG"), "bags"),
    **dict.fromkeys(
        (
            "HAT",
            "WALLET",
            "EYEWEAR",
            "SUNGLASSES",
            "WATCH",
            "NECKLACE",
            "EARRING",
            "RING",
            "BRACELET",
            "FINERING",
            "FINEEARRING",
            "FINENECKLACEBRACELETANKLET",
            "FASHIONRING",
            "FASHIONEARRING",
            "FASHIONNECKLACEBRACELETANKLET",
        ),
        "accessories",
    ),
}
_DENIM = re.compile(r"\b(farmer\w*|jeans?|denim)\b")

# Canonical colours are the English colour keys of text_match.TRANSLATIONS.
COLOURS = (
    "black",
    "white",
    "red",
    "blue",
    "navy",
    "green",
    "yellow",
    "brown",
    "grey",
    "beige",
    "pink",
    "purple",
    "orange",
    "cream",
    "burgundy",
)
# Folded colour words (English and Hungarian) → canonical colour.
COLOUR_WORDS = {
    **{colour: colour for colour in COLOURS},
    "gray": "grey",
    "fekete": "black",
    "feher": "white",
    "piros": "red",
    "voros": "red",
    "kek": "blue",
    "sotetkek": "navy",
    "zold": "green",
    "sarga": "yellow",
    "barna": "brown",
    "szurke": "grey",
    "bezs": "beige",
    "rozsaszin": "pink",
    "rozsaszinu": "pink",
    "lila": "purple",
    "narancs": "orange",
    "narancssarga": "orange",
    "krem": "cream",
    "bordo": "burgundy",
    "burgundia": "burgundy",
    "tengereszkek": "navy",
    "homok": "beige",
    "nude": "beige",
    "ekru": "cream",
    "menta": "green",
    "oliva": "green",
    "olivazold": "green",
    "mustar": "yellow",
    "antracit": "grey",
    "konyak": "brown",
    "ivory": "cream",
    "tan": "brown",
    "camel": "brown",
    "khaki": "beige",
    "taupe": "beige",
    "wine": "burgundy",
    "maroon": "burgundy",
}
# Hungarian shade compounds: világoskék (light blue) is blue; sötétkék is listed as navy.
_SHADE_PREFIXES = ("vilagos", "sotet", "halvany", "mely", "neon", "pasztell")


def colour_in(text: str | None) -> str | None:
    """The first colour named in ``text``, as a canonical English colour."""
    for word in re.findall(r"\w+", fold(text or "")):
        if word in COLOUR_WORDS:
            return COLOUR_WORDS[word]
        for prefix in _SHADE_PREFIXES:
            if word.startswith(prefix) and word[len(prefix) :] in COLOUR_WORDS:
                return COLOUR_WORDS[word[len(prefix) :]]
    return None


def glami_category(category_name: str, name: str) -> str | None:
    category = GLAMI_CATEGORIES.get(category_name)
    if category == "trousers" and _DENIM.search(fold(name)):
        return "jeans"
    return category


def glami_gender(category_name: str) -> str | None:
    prefix = category_name.split("-", 1)[0]
    return {
        "womens": "women",
        "women": "women",
        "mens": "men",
        "men": "men",
        "boys": "kids",
        "girls": "kids",
    }.get(prefix)


def title_gender(title: str) -> str | None:
    words = set(re.findall(r"[a-z]+", title.lower().replace("'", "")))
    if words & {"women", "womens", "woman", "ladies"}:
        return "women"
    if words & {"men", "mens", "man"}:
        return "men"
    if words & {"girls", "boys", "kids", "baby", "toddler", "girl", "boy"}:
        return "kids"
    return None


_SIZE_PART = re.compile(
    r"\d|\b(x{0,3}s|s|m|l|x{0,3}l|small|medium|large|x-large|xx-large|one size|regular|wide)\b",
    re.IGNORECASE,
)


_BRAND_PREFIX = re.compile(r"^amazon brand\s*[-–—]\s*", re.IGNORECASE)
_INLINE_SIZE = re.compile(r"\s*[-–]?\s*\d+(\.\d+)?\s*(UK|US|EU)\b.*$")


def abo_title(name: str) -> str:
    """Drop trailing size parts: ``Sandal, Taupe, 6.5 B US`` → ``Sandal, Taupe``."""
    name = _INLINE_SIZE.sub("", _BRAND_PREFIX.sub("", name.strip()))
    parts = [part.strip() for part in name.split(",")]
    kept = [parts[0]]
    for part in parts[1:]:
        if _SIZE_PART.search(part):
            break
        kept.append(part)
    return ", ".join(part for part in kept if part)


def model_key(brand: str | None, title: str) -> str:
    """Colour variants of one model share this key: brand plus the name before its first comma."""
    head = title.split(",", 1)[0]
    words = re.findall(r"[a-z]+", fold(f"{brand or ''} {head}"))
    return " ".join(word for word in words if word not in COLOUR_WORDS)


def stable_fraction(*parts: str) -> float:
    digest = hashlib.sha256("\x1f".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


# Synthetic HUF price bands per category, ending in 990 like Hungarian shop prices.
PRICE_BANDS = {
    "coats": (19_990, 59_990),
    "jackets": (12_990, 39_990),
    "dresses": (7_990, 24_990),
    "sweaters": (5_990, 17_990),
    "shirts": (5_990, 14_990),
    "blouses": (4_990, 12_990),
    "tops": (2_990, 8_990),
    "trousers": (6_990, 17_990),
    "jeans": (7_990, 19_990),
    "skirts": (4_990, 14_990),
    "shorts": (3_990, 9_990),
    "shoes": (9_990, 39_990),
    "bags": (6_990, 34_990),
    "accessories": (1_990, 14_990),
}
CLOTHING_SIZES = ("XS", "S", "M", "L", "XL")
SHOE_SIZES = tuple(str(size) for size in range(36, 46))


def synthetic_price(practice_id: str, category: str) -> Decimal:
    low, high = PRICE_BANDS[category]
    steps = (high - low) // 1000
    return Decimal(low + 1000 * round(stable_fraction("price", practice_id) * steps))


def synthetic_sizes(practice_id: str, category: str) -> list[str]:
    """A contiguous run of plausible sizes; bags and accessories are one size."""
    if category in {"bags", "accessories"}:
        return []
    options = SHOE_SIZES if category == "shoes" else CLOTHING_SIZES
    span = 3 + int(stable_fraction("span", practice_id) * (len(options) - 2))
    span = min(span, len(options))
    start = int(stable_fraction("start", practice_id) * (len(options) - span + 1))
    return list(options[start : start + span])


class PracticeImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(pattern=r"^https://[^\s/@]+/\S+$", max_length=500)
    path: str = Field(pattern=r"^images/(abo|glami)/[A-Za-z0-9_-]+\.jpg$")
    view: Literal["main", "other"]


class PracticeProduct(BaseModel):
    """One normalised product line of ``catalogue.jsonl``."""

    model_config = ConfigDict(extra="forbid")

    practice_id: str = Field(pattern=r"^(abo|glami)-[A-Za-z0-9]+$")
    dataset: Literal["abo", "glami"]
    group_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    language: Literal["en", "hu"]
    brand: str | None = Field(default=None, max_length=120)
    category: str
    source_category: str = Field(min_length=1, max_length=80)
    gender: Literal["women", "men", "kids"] | None = None
    colour: str | None = None
    colour_names: list[str] = Field(default_factory=list, max_length=4)
    description: str | None = Field(default=None, max_length=1500)
    price_huf: Decimal
    sizes: list[str] = Field(default_factory=list, max_length=12)
    images: list[PracticeImage] = Field(min_length=1, max_length=8)
    credit: str = Field(min_length=1, max_length=200)

    @field_validator("category")
    @classmethod
    def known_category(cls, value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError(f"unknown category: {value}")
        return value

    @field_validator("colour")
    @classmethod
    def known_colour(cls, value: str | None) -> str | None:
        if value is not None and value not in COLOURS:
            raise ValueError(f"unknown colour: {value}")
        return value


ABO_CREDIT = "Practice data: ABO © Amazon.com, CC BY 4.0"
GLAMI_CREDIT = "Practice data: GLAMI-1M © Inspigroup s.r.o., Apache-2.0"

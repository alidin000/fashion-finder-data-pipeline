"""Field-level clean-up shared by web collection and feeds.

Every function here is pure and conservative: it maps values it recognises and
returns ``None`` (or leaves the value out) for anything ambiguous, so a source
never gains a guessed price, stock state or colour.
"""

from __future__ import annotations

import html
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

# Currency words and symbols seen around Hungarian and euro prices.
CURRENCY_TOKENS = {
    "ft": "HUF",
    "ft.": "HUF",
    "huf": "HUF",
    "forint": "HUF",
    "eur": "EUR",
    "€": "EUR",
    "euro": "EUR",
}
_CURRENCY = r"(?:Ft\.?|HUF|forint|EUR|€|euro)"
_SPACES = "    "


def _currency_of(token: str) -> str:
    return CURRENCY_TOKENS[token.casefold()]


def parse_price_text(value: Any, currency: str | None = None) -> tuple[Decimal, str | None]:
    """Parse one price such as ``12 990 Ft``, ``12.990,- Ft``, ``HUF 12990`` or ``29,99 €``.

    Returns the amount and the currency named in the text (or ``currency`` when the
    text names none). A currency word that contradicts ``currency`` is an error,
    never silently dropped: ``29.99 EUR`` is not a HUF price. Ranges, "from" prices
    and ambiguous separators raise ``ValueError``.
    """
    if isinstance(value, bool) or value is None:
        raise TypeError("price must be text or a number")
    if isinstance(value, float):
        raise TypeError("binary floating-point prices are not accepted")
    if isinstance(value, int | Decimal):
        amount = Decimal(value)
        named = None
    else:
        raw = str(value)
        for space in _SPACES:
            raw = raw.replace(space, " ")
        raw = raw.strip()
        named = None
        match = re.fullmatch(rf"({_CURRENCY})\s*(.+)", raw, re.IGNORECASE) or re.fullmatch(
            rf"(.+?)\s*({_CURRENCY})", raw, re.IGNORECASE
        )
        if match:
            first, second = match.groups()
            token, raw = (
                (first, second)
                if re.fullmatch(_CURRENCY, first, re.IGNORECASE)
                else (second, first)
            )
            named = _currency_of(token.strip())
        raw = re.sub(r"[,.]-$", "", raw.strip()).replace(" ", "")
        expected = named or (currency.strip().upper() if currency else None)
        if expected == "HUF" and re.fullmatch(r"\d{1,3}(?:([,.])\d{3})(?:\1\d{3})*", raw):
            raw = raw.replace(",", "").replace(".", "")
        elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", raw):  # 12.990,00
            raw = raw.replace(".", "")
        elif re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d{1,2}", raw):  # 12,990.00
            raw = raw.replace(",", "")
        if not re.fullmatch(r"\d+(?:[.,]\d{1,2})?", raw):
            raise ValueError(f"invalid or ambiguous price: {value!r}")
        try:
            amount = Decimal(raw.replace(",", "."))
        except InvalidOperation as exc:
            raise ValueError("invalid price") from exc
    if currency and named and named != currency.strip().upper():
        raise ValueError(f"price names {named} but the offer currency is {currency}")
    if not amount.is_finite() or amount < 0:
        raise ValueError("price must be finite and nonnegative")
    return amount, named or (currency.strip().upper() if currency else None)


def sale_and_regular(
    prices: list[Any], currency: str | None = None
) -> tuple[Decimal | None, Decimal | None]:
    """Current and struck-through price from the price texts of one product card.

    One distinct amount is the current price. Two distinct amounts are read as a
    sale (lower) and the regular price (higher). More than two is ambiguous.
    """
    amounts = sorted({parse_price_text(price, currency)[0] for price in prices if price})
    if not amounts:
        return None, None
    if len(amounts) == 1:
        return amounts[0], None
    if len(amounts) == 2:
        return amounts[0], amounts[1]
    raise ValueError("more than two distinct prices on one product card")


# schema.org ItemAvailability short names and common feed or shop wording.
AVAILABILITY = {
    "instock": "in_stock",
    "in_stock": "in_stock",
    "in stock": "in_stock",
    "limitedavailability": "in_stock",
    "onlineonly": "in_stock",
    "available": "in_stock",
    "raktáron": "in_stock",
    "raktaron": "in_stock",
    "készleten": "in_stock",
    "keszleten": "in_stock",
    "elérhető": "in_stock",
    "elerheto": "in_stock",
    "kapható": "in_stock",
    "kaphato": "in_stock",
    "outofstock": "out_of_stock",
    "out_of_stock": "out_of_stock",
    "out of stock": "out_of_stock",
    "soldout": "out_of_stock",
    "sold out": "out_of_stock",
    "discontinued": "out_of_stock",
    "unavailable": "out_of_stock",
    "not available": "out_of_stock",
    "elfogyott": "out_of_stock",
    "nincs készleten": "out_of_stock",
    "nincs keszleten": "out_of_stock",
    "nem elérhető": "out_of_stock",
    "nem elerheto": "out_of_stock",
    "nem kapható": "out_of_stock",
    "nem kaphato": "out_of_stock",
    # PreOrder, BackOrder, MadeToOrder, InStoreOnly, Reserved: cannot be bought online
    # now, so they stay "unknown" rather than claiming stock.
}


def normalise_availability(value: Any) -> str:
    """``in_stock``, ``out_of_stock`` or ``unknown``; booleans are stock flags."""
    if value is True:
        return "in_stock"
    if value is False:
        return "out_of_stock"
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    text = value.strip().rstrip("/").rsplit("/", 1)[-1] if "schema.org" in value else value
    text = re.sub(r"[\s_-]+", " ", text.strip().casefold())
    return AVAILABILITY.get(text) or AVAILABILITY.get(text.replace(" ", "")) or "unknown"


def clean_text(value: Any) -> str | None:
    """Unescape HTML entities, drop tags and collapse whitespace."""
    if value is None or isinstance(value, dict | list):
        return None
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]{0,500}>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


# Hungarian and English colour words mapped to one canonical English key.
COLOUR_KEYS = {
    "black": "black",
    "fekete": "black",
    "white": "white",
    "fehér": "white",
    "feher": "white",
    "ekrü": "white",
    "ecru": "white",
    "red": "red",
    "piros": "red",
    "vörös": "red",
    "voros": "red",
    "bordó": "burgundy",
    "bordo": "burgundy",
    "burgundy": "burgundy",
    "blue": "blue",
    "kék": "blue",
    "kek": "blue",
    "világoskék": "blue",
    "vilagoskek": "blue",
    "sötétkék": "navy",
    "sotetkek": "navy",
    "navy": "navy",
    "tengerészkék": "navy",
    "green": "green",
    "zöld": "green",
    "zold": "green",
    "khaki": "khaki",
    "keki": "khaki",
    "olive": "khaki",
    "olíva": "khaki",
    "oliva": "khaki",
    "yellow": "yellow",
    "sárga": "yellow",
    "sarga": "yellow",
    "orange": "orange",
    "narancs": "orange",
    "narancssárga": "orange",
    "narancssarga": "orange",
    "brown": "brown",
    "barna": "brown",
    "camel": "brown",
    "beige": "beige",
    "bézs": "beige",
    "bezs": "beige",
    "krém": "beige",
    "krem": "beige",
    "cream": "beige",
    "grey": "grey",
    "gray": "grey",
    "szürke": "grey",
    "szurke": "grey",
    "antracit": "grey",
    "pink": "pink",
    "rózsaszín": "pink",
    "rozsaszin": "pink",
    "púder": "pink",
    "puder": "pink",
    "purple": "purple",
    "lila": "purple",
    "gold": "gold",
    "arany": "gold",
    "silver": "silver",
    "ezüst": "silver",
    "ezust": "silver",
    "multicolour": "multi",
    "multicolor": "multi",
    "tarka": "multi",
    "színes": "multi",
    "szines": "multi",
}
_COLOUR_SPLIT = re.compile(r"\s*(?:/|,|;|\+|&|\bés\b|\band\b)\s*", re.IGNORECASE)


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def colour_key(label: str) -> str | None:
    """Canonical English key of a colour label, e.g. ``Sötétkék`` -> ``navy``."""
    text = label.strip().casefold()
    if text in COLOUR_KEYS:
        return COLOUR_KEYS[text]
    folded = _fold(text)
    if folded in COLOUR_KEYS:
        return COLOUR_KEYS[folded]
    words = [COLOUR_KEYS.get(word) or COLOUR_KEYS.get(_fold(word)) for word in text.split()]
    found = {word for word in words if word}
    return found.pop() if len(found) == 1 else None


def clean_colours(value: Any) -> list[str]:
    """Split ``Red/Green`` style values, drop codes and blanks, keep the source wording."""
    values = value if isinstance(value, list) else [value] if value else []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str | int):
            continue
        for part in _COLOUR_SPLIT.split(str(item)):
            label = clean_text(part)
            if not label or re.fullmatch(
                r"[\W\d_]+|[0-9]{1,3}[A-Z]{1,3}|n/?a|-", label, re.IGNORECASE
            ):
                continue
            if label.casefold() not in seen:
                seen.add(label.casefold())
                result.append(label)
    return result


def clean_list(value: Any) -> list[str]:
    """De-duplicated non-empty strings in their original order."""
    values = value if isinstance(value, list) else [value] if value else []
    result: list[str] = []
    for item in values:
        if isinstance(item, dict | list) or item is None:
            continue
        text = clean_text(item)
        if text and text not in result:
            result.append(text)
    return result

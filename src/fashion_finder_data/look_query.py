"""Read a look-search query: price limits, hints and the English text to embed.

``piros nyári ruha 10000 Ft alatt`` becomes the English ``red summer dress``, a
colour hint (red), a category hint (dresses) and a 10,000 Ft price limit. Every
typed word is either translated, reported as a hint, or passed through as typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from .hungarian import Word, analyse, english_text, looks_hungarian

TRANSLATE_MODES = ("none", "dictionary", "dictionary+mt", "mt")
MAX_PRICE = Decimal(10_000_000)

_NUMBER = r"(\d{1,3}(?:[ . ]\d{3})+|\d+(?:[.,]\d+)?)\s*(e|k|ezer)?\b"
_CURRENCY = r"(?:\s*-?\s*(?:ft|forint|huf)\w*)"
_HIGH = r"(?:alatt|alatti|-?ig|max|maximum|or less|or under|-?n[aá]l\s+olcs[oó]bb)"
_LOW = r"(?:f[eö]l[eö]tt|f[eö]l[eö]tti|-?t[oó]l|-?t[oő]l|or more|-?n[aá]l\s+dr[aá]g[aá]bb)"
_PRICE_PATTERNS = (
    (
        "range",
        (
            rf"(?:between\s+)?{_NUMBER}{_CURRENCY}?\s*(?:-|–|és|es|and|to)\s*"
            rf"{_NUMBER}{_CURRENCY}?(?:\s*(?:k[oö]z[oö]tt|k[oö]z[oö]tti))?"
        ),
    ),
    (
        "high",
        (
            rf"(?:under|below|max|maximum|legfeljebb|up to|less than|cheaper than|"
            rf"olcs[oó]bb\s+mint|kevesebb\s+mint)\s*:?\s*{_NUMBER}{_CURRENCY}?"
        ),
    ),
    ("high", rf"{_NUMBER}{_CURRENCY}?\s*{_HIGH}"),
    (
        "low",
        (
            rf"(?:over|above|min|minimum|legal[aá]bb|more than|at least|from|"
            rf"dr[aá]g[aá]bb\s+mint)\s*:?\s*{_NUMBER}{_CURRENCY}?"
        ),
    ),
    ("low", rf"{_NUMBER}{_CURRENCY}?\s*{_LOW}"),
    ("high", rf"{_NUMBER}{_CURRENCY}"),  # a bare amount in forints is a budget
)
_PRICE_RES = [
    (kind, re.compile(rf"(?<![\w.]){pattern}(?!\w)", re.IGNORECASE))
    for kind, pattern in _PRICE_PATTERNS
]
_SIZE_RE = re.compile(
    # "38-as", "38as", "M-es" (a letter size needs the hyphen, or "más" would be M),
    # "méret: 38", "size 38" or "S méret".
    r"(?<!\w)(?:(\d{2}(?=\s*-?\s*[aeoöáé]s)|(?:xxs|xs|s|m|l|xl|xxl|xxxl)(?=\s*-\s*[aeoöáé]s))"
    r"\s*-?\s*[aeoöáé]s(?:\s+m[eé]ret\w*)?"
    r"|(?:m[eé]ret\w*|size)\s*:?\s*(\d{2}|xxs|xs|s|m|l|xl|xxl|xxxl)"
    r"|(\d{2}|xxs|xs|s|m|l|xl|xxl|xxxl)\s+m[eé]ret\w*)(?!\w)",
    re.IGNORECASE,
)


class Translator(Protocol):
    version: str

    def translate(self, text: str) -> str: ...


@dataclass(frozen=True)
class LookQuery:
    """What the shopper asked for, as the model and the ranking see it."""

    text: str
    english: str
    words: tuple[Word, ...] = ()
    translated_by: str = "none"
    colour: str | None = None
    colour_words: tuple[str, ...] = ()
    category: str | None = None
    gender: str | None = None
    size: str | None = None
    max_price: Decimal | None = None
    min_price: Decimal | None = None
    untranslated: tuple[str, ...] = ()
    machine_translated: tuple[tuple[str, str], ...] = field(default=())

    def as_response(self) -> dict[str, Any]:
        """The interpretation the shopper page can show next to the results."""
        return {
            "language": "hu" if self._hungarian else "en",
            "translated_by": self._translated_by,
            "colour": self.colour,
            "category": self.category,
            "gender": self.gender,
            "size": self.size,
            "max_price": str(self.max_price) if self.max_price is not None else None,
            "min_price": str(self.min_price) if self.min_price is not None else None,
            "untranslated": list(self.untranslated),
        }

    @property
    def _translated_by(self) -> str:
        """What actually translated this query, not merely what was allowed."""
        if self.translated_by == "mt" and self.machine_translated:
            return "mt"
        if self.machine_translated:
            return "dictionary+mt"
        if self.translated_by != "none" and any(word.language == "hu" for word in self.words):
            return "dictionary"
        return "none"

    @property
    def _hungarian(self) -> bool:
        return any(word.language == "hu" for word in self.words) or bool(self.machine_translated)


def _amount(number: str, multiplier: str | None) -> Decimal | None:
    digits = re.sub(r"[ . ]", "", number) if re.search(r"[ . ]\d{3}", number) else number
    try:
        value = Decimal(digits.replace(",", "."))
    except ArithmeticError:
        return None
    if multiplier:
        value *= 1000
    if not value.is_finite() or value <= 0 or value > MAX_PRICE:
        return None
    return value.quantize(Decimal(1))


def extract_prices(text: str) -> tuple[str, Decimal | None, Decimal | None]:
    """Remove price phrases; return the rest of the text and the limits found."""
    high = low = None
    for kind, pattern in _PRICE_RES:
        match = pattern.search(text)
        if match is None:
            continue
        groups = match.groups()
        if kind == "range":
            first, second = _amount(groups[0], groups[1]), _amount(groups[2], groups[3])
            has_currency = re.search(r"ft|forint|huf|k[oö]z[oö]tt|between", match[0], re.IGNORECASE)
            if first is None or second is None or not has_currency:
                continue
            low, high = min(first, second), max(first, second)
        else:
            value = _amount(groups[0], groups[1])
            if value is None:
                continue
            if kind == "high" and high is None:
                high = value
            elif kind == "low" and low is None:
                low = value
            else:
                continue
        text = f"{text[: match.start()]} {text[match.end() :]}"
    return " ".join(text.split()), high, low


def extract_size(text: str) -> tuple[str, str | None]:
    match = _SIZE_RE.search(text)
    if match is None:
        return text, None
    size = (match[1] or match[2] or match[3]).upper()
    return " ".join(f"{text[: match.start()]} {text[match.end() :]}".split()), size


_PARENTHESES = re.compile(r"\([^)]*\)")


def _clean_translation(source: str, translated: str) -> str:
    """Drop what a translation model invents: brackets and numbers not in the source."""
    text = _PARENTHESES.sub(" ", translated)
    words = [
        word
        for word in re.findall(r"[\w'’-]+", text)
        if not any(char.isdigit() for char in word) or word in source
    ]
    return " ".join(words[: 3 * max(1, len(source.split()))]).lower()


def _machine_translate(
    words: list[Word], translator: Translator
) -> tuple[list[Word], list[tuple[str, str]]]:
    """Translate each run of unknown Hungarian words; known words keep their sense.

    Only words with Hungarian letters go to the model: it rewrites brand names
    ("Manzara" became "silsara"), and brands are usually plain ASCII.
    """
    result: list[Word] = []
    pairs: list[tuple[str, str]] = []
    run: list[Word] = []

    def flush() -> None:
        if not run:
            return
        source = " ".join(word.text for word in run)
        english = _clean_translation(source, translator.translate(source))
        if english and english != source.lower():
            pairs.append((source, english))
            result.append(Word(source, english, "machine", "hu"))
        else:
            result.extend(run)
        run.clear()

    for word in words:
        if word.kind == "unknown" and looks_hungarian(word.text):
            run.append(word)
        else:
            flush()
            result.append(word)
    flush()
    return result, pairs


def understand(
    text: str,
    *,
    translate: str = "dictionary",
    translator: Translator | None = None,
    price_hints: bool = True,
) -> LookQuery:
    """Read a shopper's look description; see the module docstring."""
    if translate not in TRANSLATE_MODES:
        raise ValueError(f"translate must be one of {', '.join(TRANSLATE_MODES)}")
    if "mt" in translate and translator is None:
        raise ValueError("machine translation needs a translation model directory")
    rest, high, low = extract_prices(text) if price_hints else (text, None, None)
    rest, size = extract_size(rest) if price_hints else (rest, None)
    if translate == "none":
        # The words still drive hints and word relevance; only the embedding is untranslated.
        english = " ".join(rest.split())
        words = analyse(rest)
        return _with_hints(
            LookQuery(text, english, tuple(words), max_price=high, min_price=low, size=size)
        )
    if translate == "mt":
        english = _clean_translation(rest, translator.translate(rest)) if rest.strip() else ""
        words = analyse(rest)  # for hints only
        return _with_hints(
            LookQuery(
                text,
                english or rest,
                tuple(words),
                "mt",
                max_price=high,
                min_price=low,
                size=size,
                machine_translated=((rest, english),) if english else (),
            )
        )
    words = analyse(rest)
    pairs: list[tuple[str, str]] = []
    if translate == "dictionary+mt" and any(word.kind == "unknown" for word in words):
        # Unknown words with Hungarian letters go to the translation model.
        words, pairs = _machine_translate(words, translator)
    untranslated = tuple(
        word.text for word in words if word.kind == "unknown" and looks_hungarian(word.text)
    )
    return _with_hints(
        LookQuery(
            text,
            english_text(words),
            tuple(words),
            translate,
            max_price=high,
            min_price=low,
            size=size,
            untranslated=untranslated,
            machine_translated=tuple(pairs),
        )
    )


def _with_hints(query: LookQuery) -> LookQuery:
    colours = [word for word in query.words if word.kind == "colour"]
    categories = {word.category for word in query.words if word.category}
    genders = [word.gender for word in query.words if word.gender]
    return LookQuery(
        **{
            **query.__dict__,
            "colour": colours[0].english if colours else None,
            "colour_words": tuple(
                value for word in colours for value in (word.lemma, word.english) if value
            ),
            "category": categories.pop() if len(categories) == 1 else None,
            "gender": genders[0] if genders else None,
        }
    )

"""Reading Hungarian look queries: endings, compounds, prices, hints and fallbacks."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from fashion_finder_data.hungarian import analyse
from fashion_finder_data.look_query import understand
from fashion_finder_data.look_search import _lexical_scores, embed, lexical_terms, templated
from fashion_finder_data.text_match import english_query, matches_terms, query_terms
from fashion_finder_data.translation import OPUS_MT_FILES, get_translator


@pytest.mark.parametrize(
    ("typed", "english"),
    [
        ("kabátokat", "coat"),  # plural + accusative
        ("Fekete kabátok", "black coat"),
        ("ruhát", "dress"),  # accusative after a lengthened vowel, accents optional
        ("ruhaban", "dress"),
        ("cipőt", "shoes"),
        ("kabáttal", "coat"),  # assimilated instrumental
        ("nőknek csizmát", "women's boots"),
        ("pirosat", "red"),
        ("bőrkabátot", "leather coat"),  # compound + ending
        ("farmerdzsekit", "denim jacket"),
        ("selyemblúz", "silk blouse"),  # compound split, not listed
        ("fekete-fehér csíkos póló", "black and white striped t-shirt"),
        ("hosszú ujjú kötött pulóver", "long sleeve knitted sweater"),
        ("magas derekú széles szárú nadrág", "high-waisted wide-leg trousers"),
        ("kapucnis pulóvert", "hoodie"),
        ("black leather jacket", "black leather jacket"),  # English passes through
    ],
)
def test_hungarian_endings_compounds_and_phrases_reach_the_model_in_english(
    typed: str, english: str
) -> None:
    assert english_query(typed) == english


def test_unknown_words_are_kept_as_typed_and_reported() -> None:
    query = understand("Tamaris bagolymintás póló")
    # A brand and an unlisted word are never dropped; only the known word is translated.
    assert query.english == "Tamaris bagolymintás t-shirt"
    assert query.untranslated == ("bagolymintás",)
    assert [word.kind for word in analyse("a piros ruha")] == ["filler", "colour", "garment"]


def test_short_hungarian_words_do_not_swallow_brands_or_english() -> None:
    assert understand("Zara Desigual linen dress").english == "Zara Desigual linen dress"
    assert understand("women's t-shirt").english == "women's t-shirt"


@pytest.mark.parametrize(
    ("typed", "english", "high", "low"),
    [
        ("piros nyári ruha 10000 Ft alatt", "red summer dress", 10000, None),
        ("fekete kabát 20 000 Ft-ig", "black coat", 20000, None),
        ("csizma 10e Ft alatt", "boots", 10000, None),
        ("black jacket under 15000 Ft", "black jacket", 15000, None),
        ("5000 és 10000 Ft között szoknya", "skirt", 10000, 5000),
        ("10000 forintnál olcsóbb táska", "bag", 10000, None),
        ("ruha 15000 felett", "dress", None, 15000),
        ("Levi's 501 jeans", "Levi's 501 jeans", None, None),  # a model number, not a price
    ],
)
def test_price_phrases_become_limits_and_leave_the_description(
    typed: str, english: str, high: int | None, low: int | None
) -> None:
    query = understand(typed)
    assert query.english == english
    assert query.max_price == (Decimal(high) if high else None)
    assert query.min_price == (Decimal(low) if low else None)


def test_hints_name_colour_category_gender_and_size() -> None:
    query = understand("38-as női piros ruha")
    assert (query.colour, query.category, query.gender, query.size) == (
        "red",
        "dresses",
        "women",
        "38",
    )
    assert "piros" in query.colour_words  # the dictionary form matches Hungarian colours
    english = understand("red dress", translate="dictionary+mt", translator=Dictionary())
    assert english.as_response()["language"] == "en"
    assert english.as_response()["translated_by"] == "none"  # nothing needed translating
    assert understand("S méret póló").size == "S"
    assert understand("más ruha").size is None  # "más" (other) is not size M
    # Two garments, no single category hint.
    assert understand("kabát és sál").category is None
    # With price hints off, the words are kept as typed.
    assert understand("ruha 10000 Ft alatt", price_hints=False).max_price is None


class Dictionary:
    """Stand-in translation model: a fixed phrase book, plus an invented number."""

    version = "stand-in"

    def __init__(self) -> None:
        self.asked: list[str] = []

    def translate(self, text: str) -> str:
        self.asked.append(text)
        return {"bagolymintás": "owl print (1999)", "zsiráf": "zsiráf"}.get(text, text)


def test_translation_model_only_fills_gaps_and_its_inventions_are_dropped() -> None:
    model = Dictionary()
    query = understand(
        "Manzara piros bagolymintás póló", translate="dictionary+mt", translator=model
    )
    # Dictionary words and plain-ASCII brands never reach the model.
    assert model.asked == ["bagolymintás"]
    assert query.english == "Manzara red owl print t-shirt"
    assert query.translated_by == "dictionary+mt"
    assert query.untranslated == ()
    # An echo is no translation: the typed word is kept and reported.
    echoed = understand("zsiráf ruha", translate="dictionary+mt", translator=model)
    assert echoed.english == "zsiráf dress"
    with pytest.raises(ValueError, match="translation model"):
        understand("ruha", translate="dictionary+mt")
    with pytest.raises(ValueError, match="translate must be"):
        understand("ruha", translate="google")


def test_whole_query_translation_and_no_translation_modes() -> None:
    model = Dictionary()
    assert understand("ruha 9000 Ft alatt", translate="none").english == "ruha"
    whole = understand("bagolymintás", translate="mt", translator=model)
    assert whole.english == "owl print"
    assert whole.translated_by == "mt"


def test_translation_model_files_must_be_the_pinned_ones(tmp_path: Path) -> None:
    for name in OPUS_MT_FILES:
        (tmp_path / name).write_bytes(b"not a model")
    with pytest.raises(ValueError, match="does not match the pinned"):
        get_translator(tmp_path)
    with pytest.raises(ValueError, match="needs a translation model"):
        get_translator(None)


class Axes:
    """Stand-in text tower: each known word is one axis."""

    length = 3

    def encode(self, text: str) -> list[float]:
        return {"a red": [1.0, 0.0, 0.0], "b red": [0.0, 1.0, 0.0]}[text]


def test_prompt_templates_wrap_and_average_the_query() -> None:
    assert templated("red dress", "a photo of {q}") == ["a photo of red dress"]
    assert templated("red dress", None) == ["red dress"]
    averaged = embed(Axes(), templated("red", ["a {q}", "b {q}"]))
    assert averaged == pytest.approx([2**-0.5, 2**-0.5, 0.0])
    with pytest.raises(ValueError, match=r"\{q\}"):
        templated("red dress", "a photo")


def test_word_matching_understands_endings_and_more_english() -> None:
    def hit(query: str, title: str) -> bool:
        return matches_terms(query_terms(query), title)

    assert hit("kabátot", "Steppelt kabát")  # the shopper's ending, the title's lemma
    assert hit("fekete nadrágokat", "Black trousers")  # Hungarian query, English title
    assert hit("boots", "Női bőr csizma")  # English query, Hungarian title
    assert hit("leather belt", "Bőr öv")
    assert hit("belt", "Övek")  # a short word with a plural ending
    assert not hit("leather", "Bordó ruha")  # "bőr" is not the start of "bordó"
    assert not hit("belt", "Overál")


def offer(offer_id: int, title: str, category: str | None = None) -> dict:
    return {"id": offer_id, "title": title, "category": category, "colour_names": []}


def test_word_relevance_keeps_only_offers_with_the_most_query_words() -> None:
    offers = [
        offer(1, "Manzara PARKA KABÁT piros", "coats"),
        offer(2, "Piros ruha", "dresses"),
        offer(3, "Fekete kabát", "coats"),
    ]

    def lexical(text: str) -> set[int]:
        return set(_lexical_scores(lexical_terms(understand(text)), offers))

    # One offer names both words, so a red dress no longer competes on words.
    assert lexical("manzara piros") == {1}
    assert lexical("piros kabátot") == {1}
    # No offer names "zsiráf": the offers with the most words stand in.
    assert lexical("piros zsiráf") == {1, 2}

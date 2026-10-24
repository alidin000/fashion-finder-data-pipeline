"""HUF prices, stock, colours and categories are normalised without guessing."""

from __future__ import annotations

from decimal import Decimal

import pytest

from fashion_finder_data.category_mapping import (
    category_from_path,
    category_from_slug_extended,
    map_category,
)
from fashion_finder_data.collectors import source_for_page
from fashion_finder_data.normalisation import (
    clean_colours,
    colour_key,
    normalise_availability,
    parse_price_text,
    sale_and_regular,
)
from fashion_finder_data.schema import parse_money
from fashion_finder_data.sources import SOURCES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12 990 Ft", Decimal(12990)),
        ("12 990 Ft", Decimal(12990)),
        ("12 990 Ft", Decimal(12990)),
        ("12.990 Ft", Decimal(12990)),
        ("12 990,- Ft", Decimal(12990)),
        ("12.990,00 Ft", Decimal("12990.00")),
        ("1.299.990 Ft", Decimal(1299990)),
        ("HUF 12990", Decimal(12990)),
        ("Ft 12 990", Decimal(12990)),
        ("12990 forint", Decimal(12990)),
        ("24750,0", Decimal("24750.0")),
        (Decimal("39995.00"), Decimal("39995.00")),
    ],
)
def test_hungarian_price_formats(text, expected) -> None:
    assert parse_price_text(text, "HUF")[0] == expected


@pytest.mark.parametrize(
    "text",
    ["12 990 - 15 990 Ft", "12 990 Ft-tól", "akció", "1,234", "-500 Ft", "12.99.0"],
)
def test_ranges_from_prices_and_ambiguous_values_are_rejected(text) -> None:
    with pytest.raises(ValueError):
        parse_price_text(text, "EUR" if text == "1,234" else "HUF")


def test_a_currency_word_that_contradicts_the_offer_is_an_error() -> None:
    # The old parser dropped any suffix, so "29.99 EUR" imported as 29.99 HUF.
    with pytest.raises(ValueError, match="EUR"):
        parse_money("29.99 EUR", "HUF")
    assert parse_money("29,99 €", "EUR") == Decimal("29.99")
    assert parse_price_text("12 990 Ft")[1] == "HUF"


def test_float_prices_are_refused() -> None:
    with pytest.raises(TypeError):
        parse_price_text(12.5, "HUF")


def test_sale_and_regular_price_from_a_card() -> None:
    assert sale_and_regular(["19 990 Ft", "13 990 Ft"], "HUF") == (Decimal(13990), Decimal(19990))
    assert sale_and_regular(["9 990 Ft", "9990 Ft"], "HUF") == (Decimal(9990), None)
    with pytest.raises(ValueError):
        sale_and_regular(["1 Ft", "2 Ft", "3 Ft"], "HUF")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://schema.org/InStock", "in_stock"),
        ("http://schema.org/OutOfStock", "out_of_stock"),
        ("in stock", "in_stock"),
        ("In Stock", "in_stock"),
        ("Raktáron", "in_stock"),
        ("Elfogyott", "out_of_stock"),
        ("nincs készleten", "out_of_stock"),
        ("https://schema.org/Discontinued", "out_of_stock"),
        ("https://schema.org/PreOrder", "unknown"),
        ("BackOrder", "unknown"),
        (True, "in_stock"),
        (None, "unknown"),
        ("maybe", "unknown"),
    ],
)
def test_availability_mapping(value, expected) -> None:
    assert normalise_availability(value) == expected


def test_colours_are_split_cleaned_and_keyed() -> None:
    assert clean_colours(["Red/Green", "red", " 99X ", "Fekete és fehér"]) == [
        "Red",
        "Green",
        "Fekete",
        "fehér",
    ]
    assert colour_key("Sötétkék") == "navy"
    assert colour_key("mintás") is None  # words without a known colour stay unmapped
    assert colour_key("fekete-fehér csíkos") is None  # two colours are not one key
    assert colour_key("világos szürke") == "grey"


def test_categories_from_breadcrumbs_google_paths_and_slugs() -> None:
    assert map_category(breadcrumbs=["Női", "Ruházat", "Pólók"], product_name="X") == (
        "Pólók",
        "tops",
    )
    # A crumb that names two garments stops the search instead of guessing.
    assert map_category(breadcrumbs=["Női", "Dzsekik és kabátok"])[1] is None
    assert category_from_path("Apparel & Accessories > Clothing > Dresses") == "dresses"
    assert (
        category_from_path("Apparel & Accessories > Clothing > Outerwear > Coats & Jackets") is None
    )
    assert category_from_path("Apparel & Accessories > Clothing") is None
    assert category_from_slug_extended("melegito-felsok") == "sweaters"
    assert category_from_slug_extended("k-tshirts") == "tops"
    assert category_from_slug_extended("kabatok-polok") is None


def test_house_clothing_root_is_not_labelled_dresses() -> None:
    house = SOURCES["house"]
    assert source_for_page(house, "https://www.housebrand.com/hu/hu/noi/ruhak").category is None
    assert (
        source_for_page(house, "https://www.housebrand.com/hu/hu/noi/ruhak/polok-topok").category
        == "tops"
    )

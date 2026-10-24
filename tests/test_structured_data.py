"""Generic structured-data extraction turns shop pages into honest offers."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from scrapy.http import HtmlResponse, Request

from fashion_finder_data.collectors import CrawlState, ProductSpider, extract_records
from fashion_finder_data.schema import normalize
from fashion_finder_data.sources import SOURCES
from fashion_finder_data.structured_data import extract, robots_directives

PAGES = Path(__file__).parent / "fixtures" / "pages"


def page(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


def response(url: str, name: str, **meta) -> HtmlResponse:
    return HtmlResponse(
        url=url,
        body=page(name).encode(),
        encoding="utf-8",
        request=Request(url, meta=meta),
    )


def test_product_group_variants_become_one_offer_per_colour_url() -> None:
    result = extract(
        page("ecipo-productgroup.html"),
        "https://ecipo.hu/p/bor-bokacsizma-ec7781-fekete",
        SOURCES["ecipo"],
    )
    assert result.method == "jsonld"
    black, brown = sorted(result.products, key=lambda product: product["url"])[::-1]
    assert black["url"].endswith("-fekete")
    assert black["name"] == "Bőr bokacsizma"  # " | Ecipo.hu" stripped by config
    assert black["sku"] == "EC-7781"  # two size variants share the group ID
    assert black["sizes"] == ["38", "39"]
    assert black["availability"] == "in_stock"  # one size in stock is enough
    assert (black["price"], black["original_price"]) == ("34990", "49990")
    assert black["category_key"] == "shoes" and black["category"] == "Cipők"
    assert black["images"] == [
        "https://img.ecipo.hu/media/boot-front.jpg",
        "https://img.ecipo.hu/media/boot-side.jpg",
    ]  # the size chart is dropped
    assert brown["sku"] == "EC-7781-38B" and brown["availability"] == "out_of_stock"
    offer = normalize(
        extract_records(
            response("https://ecipo.hu/p/bor-bokacsizma-ec7781-fekete", "ecipo-productgroup.html"),
            SOURCES["ecipo"],
        )[0][0]
    )
    assert offer.description == "Bélelt & vízálló bőr bokacsizma."
    assert offer.colour_names == ["Fekete"]
    assert offer.original_price_amount == Decimal(49990)


def test_listing_ignores_priceless_category_node_and_follows_product_links() -> None:
    source = SOURCES["modivo"]
    result = extract(
        page("modivo-category.html"),
        "https://modivo.hu/c/noi/ruhazat/kabatok",
        source,
        listing=True,
    )
    assert result.products == []
    spider = ProductSpider([source], CrawlState(), 5)
    requests = list(
        spider.parse_page(
            response(
                "https://modivo.hu/c/noi/ruhazat/kabatok", "modivo-category.html", kind="listing"
            ),
            source,
        )
    )
    assert [request.url for request in requests] == [
        "https://modivo.hu/p/gyapju-kabat-md1001",
        "https://modivo.hu/p/steppelt-dzseki-md1002",
    ]
    # A product page with only a price range is rejected with a reason, not guessed.
    on_product_page = extract(
        page("modivo-category.html"), "https://modivo.hu/p/gyapju-kabat-md1001", source
    )
    assert on_product_page.products == []
    assert on_product_page.rejected == ["price range without one concrete price"]


def test_localised_schema_types_and_comma_decimals_are_understood() -> None:
    result = extract(
        page("localised-types.html"), "https://sportano.hu/futokabat-sd4410", SOURCES["sportano"]
    )
    product = result.products[0]
    assert (product["price"], product["currency"]) == ("24750.0", "HUF")
    assert product["colours"] == ["Sötétkék", "Fehér"]
    assert product["brand"] == "Példa Sport"


def test_microdata_product_ignores_nested_recommendations() -> None:
    result = extract(
        page("microdata-product.html"), "https://sportano.hu/pamut-polo-sp200", SOURCES["sportano"]
    )
    assert result.method == "microdata"
    [product] = result.products
    assert product["name"] == "Pamut póló"
    assert (product["price"], product["currency"], product["sku"]) == ("7990", "HUF", "SP-200")
    assert product["availability"] == "in_stock"  # LimitedAvailability can still be bought
    assert product["images"] == ["https://sportano.hu/media/polo-main.jpg"]


def test_opengraph_fallback_reads_sale_and_regular_price() -> None:
    result = extract(
        page("opengraph-only.html"),
        "https://sportano.hu/kotott-pulover-sp77?utm_source=x",
        SOURCES["sportano"],
    )
    [product] = result.products
    assert result.method == "opengraph"
    assert (product["price"], product["original_price"]) == ("9990", "12990")
    assert product["availability"] == "out_of_stock"
    assert product["url"] == "https://sportano.hu/kotott-pulover-sp77"


def test_variants_with_different_prices_on_one_url_are_rejected() -> None:
    result = extract(
        page("variants-differ.html"), "https://sportano.hu/farmer-j1", SOURCES["sportano"]
    )
    assert result.products == []
    assert result.rejected == ["variants on one offer URL have different prices"]


def test_noai_page_stops_the_source_and_stores_nothing() -> None:
    source = SOURCES["sportano"]
    state = CrawlState()
    spider = ProductSpider([source], state, 5)
    list(
        spider.parse_page(
            response("https://sportano.hu/kabat-1", "noai-product.html", kind="product"), source
        )
    )
    assert state.records == {}
    assert spider.telemetry.stopped("sportano") == "page opts out of AI use (robots noai)"


def test_robots_directives_for_other_crawlers_do_not_apply() -> None:
    assert robots_directives(None, "googlebot: noindex, nofollow") == set()
    assert robots_directives(None, "fashionfinderbot: noai") == {"noai"}
    assert robots_directives(None, "noindex, unavailable_after: 25 Jun 2026") >= {"noindex"}

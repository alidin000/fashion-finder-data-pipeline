"""The hosted demo's policy publishes names, prices and links, never retailer photos."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fashion_finder_data.public_catalogue import search_public_catalogue
from fashion_finder_data.schema import RetailerOffer, utc_now
from fashion_finder_data.source_policy import load_policies
from fashion_finder_data.storage import open_database, upsert_offer

HOSTED_POLICY = Path(__file__).resolve().parents[1] / "config/source-policies.render-listing.json"


def test_hosted_policy_lists_the_reviewed_shops_without_any_photo_rights() -> None:
    policies = load_policies(HOSTED_POLICY)

    assert {key for key, _ in policies} == {"reserved", "sinsay", "mohito", "house", "cropp"}
    for policy in policies.values():
        assert policy.allow_public_listing
        assert not policy.allow_image_display and not policy.allow_image_processing


def test_hosted_search_shows_the_offer_but_not_its_photo(tmp_path) -> None:
    database = open_database(tmp_path / "fashion_finder.sqlite")
    url = "https://www.reserved.com/hu/hu/gyapju-kabat-101aa-99x"
    upsert_offer(
        database,
        RetailerOffer(
            source_key="reserved",
            source_url=url,
            canonical_url=url,
            source_product_id="101AA-99X",
            title="Gyapjú kabát",
            price_amount=Decimal(39995),
            currency="HUF",
            image_urls=["https://static.reserved.com/media/101AA-99X.jpg"],
            availability_status="in_stock",
            source_fetched_at=utc_now(),
        ),
    )

    items = search_public_catalogue(database, load_policies(HOSTED_POLICY), query="kabát")["items"]

    assert [item["title"] for item in items] == ["Gyapjú kabát"]
    assert items[0]["image_urls"] == []
    assert items[0]["source_url"] == url

"""The hosted demo's policy: the five reviewed shops, with photos from the shops' own servers."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from fashion_finder_data.public_catalogue import search_public_catalogue
from fashion_finder_data.schema import RetailerOffer, utc_now
from fashion_finder_data.source_policy import load_policies
from fashion_finder_data.storage import open_database, upsert_offer

HOSTED_POLICY = Path(__file__).resolve().parents[1] / "config/source-policies.render-listing.json"


def test_hosted_policy_covers_the_reviewed_shops_and_records_the_owner_decision() -> None:
    policies = load_policies(HOSTED_POLICY)

    assert {key for key, _ in policies} == {"reserved", "sinsay", "mohito", "house", "cropp"}
    for policy in policies.values():
        assert policy.allow_public_listing and policy.allow_image_display
        # The approval is an owner decision without retailer permission; it must say so.
        assert "owner decision" in policy.evidence_ref
        assert "no retailer permission" in policy.evidence_ref


def test_hosted_search_links_photos_on_the_shop_server_and_hides_foreign_ones(tmp_path) -> None:
    database = open_database(tmp_path / "fashion_finder.sqlite")
    url = "https://www.reserved.com/hu/hu/gyapju-kabat-101aa-99x"
    shop_photo = "https://static.reserved.com/media/101AA-99X.jpg"
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
            image_urls=[shop_photo, "http://static.reserved.com/insecure.jpg"],
            availability_status="in_stock",
            source_fetched_at=utc_now(),
        ),
    )

    items = search_public_catalogue(database, load_policies(HOSTED_POLICY), query="kabát")["items"]

    assert [item["title"] for item in items] == ["Gyapjú kabát"]
    # Photos are shown from the shop's own HTTPS image server, never a copy or plain HTTP.
    assert items[0]["image_urls"] == [shop_photo]
    assert all(
        urlsplit(photo).hostname.endswith("reserved.com") for photo in items[0]["image_urls"]
    )

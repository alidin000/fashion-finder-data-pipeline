"""Local XML mapping keeps raw partner exports out of the public catalogue."""

from __future__ import annotations

import csv
import gzip
import json
from datetime import UTC, datetime

import pytest

from fashion_finder_data.cli import main
from fashion_finder_data.feed import validate_feed
from fashion_finder_data.feed_mapping import map_xml_feed


@pytest.mark.parametrize(
    ("source", "url"),
    [
        ("aboutyou", "https://www.aboutyou.hu/p/label/coat-123"),
        ("answear", "https://answear.hu/p/label/coat-123"),
    ],
)
def test_xml_mapping_produces_valid_feed_without_database(tmp_path, capsys, source, url):
    xml = tmp_path / "partner.xml"
    xml.write_text(
        '<catalogue xmlns="urn:synthetic">'
        "<product><links><product-url>"
        f"{url}"
        '</product-url></links><title>Blue coat</title><price currency="HUF">12990</price>'
        '<image url="https://images.example/coat.jpg"/></product></catalogue>',
        encoding="utf-8",
    )
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "record_tag": "product",
                "fields": {
                    "product_url": "links/product-url",
                    "name": "title",
                    "price": "price",
                    "currency": "price/@currency",
                    "image_url": "image/@url",
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "mapped.csv"
    observed = datetime.now(UTC).isoformat()
    assert (
        main(
            [
                "--data-dir",
                str(tmp_path / "data"),
                "map-xml-feed",
                source,
                str(xml),
                str(mapping),
                str(output),
                "--observed-at",
                observed,
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["records_valid"] == 1
    assert validate_feed(output, source)["status"] == "valid"
    with output.open(newline="", encoding="utf-8") as stream:
        row = next(csv.DictReader(stream))
    assert row["product_url"] == url
    assert row["observed_at"] == observed
    assert row["image_url"] == "https://images.example/coat.jpg"
    assert not (tmp_path / "data").exists()


def test_bad_product_prevents_output_and_preserves_existing_file(tmp_path):
    xml = tmp_path / "partner.xml"
    xml.write_text(
        "<catalogue><product><url>https://www.aboutyou.hu/p/label/coat-1</url>"
        "<name>Coat</name><price>12990</price></product>"
        "<product><url>https://evil.example/p/coat</url><name>Other</name>"
        "<price>8000</price></product></catalogue>",
        encoding="utf-8",
    )
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "record_tag": "product",
                "fields": {"product_url": "url", "name": "name", "price": "price"},
                "constants": {"currency": "HUF"},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "aboutyou.csv"
    with pytest.raises(ValueError, match="outside the Hungarian storefront"):
        map_xml_feed("aboutyou", xml, mapping, output, observed_at=datetime.now(UTC).isoformat())
    assert not output.exists()
    assert not list(tmp_path.glob(".aboutyou.csv.*.tmp"))

    output.write_text("existing approved feed", encoding="utf-8")
    with pytest.raises(FileExistsError):
        map_xml_feed("aboutyou", xml, mapping, output, observed_at=datetime.now(UTC).isoformat())
    assert output.read_text(encoding="utf-8") == "existing approved feed"


def test_xml_entities_are_rejected_before_output(tmp_path):
    xml = tmp_path / "partner.xml"
    xml.write_text(
        '<!DOCTYPE catalogue [<!ENTITY payload "unexpected">]>'
        "<catalogue><product><name>&payload;</name></product></catalogue>",
        encoding="utf-8",
    )
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "record_tag": "product",
                "fields": {"product_url": "url", "name": "name", "price": "price"},
                "constants": {"currency": "HUF", "observed_at": datetime.now(UTC).isoformat()},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "mapped.csv"
    with pytest.raises(ValueError, match="forbidden declarations"):
        map_xml_feed("aboutyou", xml, mapping, output)
    assert not output.exists()


def test_google_style_feed_groups_sizes_falls_back_to_sale_price_and_filters(tmp_path):
    """CJ/Tradedoubler exports: one row per size, sale_price before price, many categories."""
    rows = [
        ("J1", "M", "in stock", "29990 HUF", "19990 HUF", "Apparel > Clothing > Outerwear"),
        ("J1", "L", "out of stock", "29990 HUF", "19990 HUF", "Apparel > Clothing > Outerwear"),
        ("D1", "S", "in stock", "15990 HUF", "", "Apparel > Clothing > Dresses"),
        ("H1", "", "in stock", "4990 HUF", "", "Home > Kitchen"),
    ]
    items = "".join(
        f"<item><g:item_group_id>{group}</g:item_group_id><g:id>{group}-{size}</g:id>"
        f"<title>Item {group}</title><link>https://www.aboutyou.hu/p/brand/item-{group.lower()}</link>"
        f"<g:size>{size}</g:size><g:availability>{stock}</g:availability>"
        f"<g:price>{price}</g:price><g:sale_price>{sale}</g:sale_price>"
        f"<g:product_type>{kind}</g:product_type>"
        f"<g:image_link>https://images.example/{group}.jpg</g:image_link>"
        f"<g:additional_image_link>https://images.example/{group}-2.jpg,"
        f"https://images.example/{group}-3.jpg</g:additional_image_link></item>"
        for group, size, stock, price, sale, kind in rows
    )
    xml = tmp_path / "feed.xml.gz"
    xml.write_bytes(
        gzip.compress(
            f'<rss xmlns:g="http://base.google.com/ns/1.0"><channel>{items}</channel></rss>'.encode()
        )
    )
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "record_tag": "item",
                "fields": {
                    "product_url": "link",
                    "name": "title",
                    "price": ["sale_price", "price"],
                    "original_price": "price",
                    "item_group_id": "item_group_id",
                    "sku": "id",
                    "size": "size",
                    "availability": "availability",
                    "image_url": "image_link",
                    "additional_image_urls": "additional_image_link",
                },
                "constants": {"currency": "HUF"},
                "filter": {"path": "product_type", "pattern": "Clothing"},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "mapped.csv"
    result = map_xml_feed(
        "aboutyou", xml, mapping, output, observed_at=datetime.now(UTC).isoformat()
    )
    assert (result["records"], result["filtered_out"], result["rows_merged"]) == (4, 1, 1)
    with output.open(newline="", encoding="utf-8") as stream:
        jacket, dress = csv.DictReader(stream)
    assert (jacket["sku"], jacket["size"], jacket["availability"]) == ("J1", "M|L", "in_stock")
    assert (jacket["price"], jacket["original_price"]) == ("19990 HUF", "29990 HUF")
    assert dress["price"] == "15990 HUF"  # no sale price: the regular price is current
    assert jacket["additional_image_urls"] == (
        "https://images.example/J1-2.jpg https://images.example/J1-3.jpg"
    )
    assert validate_feed(output, "aboutyou")["records_valid"] == 2


def test_allow_partial_writes_the_valid_rows_and_reports_the_rest(tmp_path):
    xml = tmp_path / "partner.xml"
    xml.write_text(
        "<catalogue><product><url>https://www.aboutyou.hu/p/label/coat-1</url>"
        "<name>Coat</name><price>12990</price></product>"
        "<product><url>https://evil.example/p/coat</url><name>Other</name>"
        "<price>8000</price></product></catalogue>",
        encoding="utf-8",
    )
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "record_tag": "product",
                "fields": {"product_url": "url", "name": "name", "price": "price"},
                "constants": {"currency": "HUF"},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "aboutyou.csv"
    result = map_xml_feed(
        "aboutyou",
        xml,
        mapping,
        output,
        observed_at=datetime.now(UTC).isoformat(),
        allow_partial=True,
    )
    assert (result["records_valid"], result["rows_rejected_before_output"]) == (1, 1)
    assert "outside the Hungarian storefront" in result["errors"][0]
    assert validate_feed(output, "aboutyou")["status"] == "valid"

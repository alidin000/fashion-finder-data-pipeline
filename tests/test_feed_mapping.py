"""Local XML mapping keeps raw partner exports out of the public catalogue."""

from __future__ import annotations

import csv
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

"""Release measurements must match public visibility without replacing human review."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fashion_finder_data.catalogue.models import RetailerOffer as StoredOffer
from fashion_finder_data.readiness import readiness_report, write_audit_sample
from fashion_finder_data.schema import RetailerOffer
from fashion_finder_data.source_policy import SourcePolicy
from fashion_finder_data.storage import open_database, upsert_offer

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


def add_offer(database, source, number, *, checked=NOW, title=None, category="jackets"):
    url = f"https://www.{source}.com/hu/hu/item-{number}"
    upsert_offer(
        database,
        RetailerOffer(
            source_key=source,
            source_product_id=str(number),
            source_url=url,
            canonical_url=url,
            title=title or f"Jacket {number}",
            category=category,
            price_amount=Decimal(9995),
            currency="HUF",
            image_urls=[f"https://www.{source}.com/images/{number}.jpg"],
            availability_status="unknown",
            source_fetched_at=checked,
        ),
    )


def approved(source, *, images=True):
    return SourcePolicy(
        source_key=source,
        approval_status="approved",
        evidence_ref="private agreement reference",
        allow_public_listing=True,
        allow_image_display=images,
        allow_image_processing=images,
        freshness_hours=24,
    )


def test_release_counts_only_current_public_offers_in_one_category(tmp_path):
    database = open_database(tmp_path / "catalog.sqlite")
    for index in range(100):
        add_offer(database, "reserved", index)
    for index in range(99):
        add_offer(database, "zara", index)
    add_offer(database, "zara", "stale", checked=NOW - timedelta(days=2))
    add_offer(database, "zara", "other-category", category="coats")
    policies = {("reserved", "HU"): approved("reserved"), ("zara", "HU"): approved("zara")}

    report = readiness_report(database, policies, "jackets", now=NOW)
    by_source = {item["source_key"]: item for item in report["sources"]}
    assert by_source["reserved"]["public_eligible_offers"] == 100
    assert by_source["zara"]["stored_offers"] == 100
    assert by_source["zara"]["public_eligible_offers"] == 99
    assert by_source["zara"]["offers_to_text_target"] == 1
    assert report["text_coverage_pass"] is False
    assert report["photo_coverage_pass"] is False
    assert report["policy_evidence_verified_by_tool"] is False
    assert report["remaining_human_checks"]
    assert "private agreement reference" not in str(report)

    add_offer(database, "zara", "stale")
    report = readiness_report(database, policies, "jackets", now=NOW)
    assert report["text_coverage_pass"] is True
    assert report["photo_coverage_pass"] is True

    policies[("zara", "HU")] = approved("zara", images=False)
    report = readiness_report(database, policies, "jackets", now=NOW)
    assert report["text_coverage_pass"] is True
    assert report["photo_coverage_pass"] is False
    assert readiness_report(database, {}, "jackets", now=NOW)["text_coverage_pass"] is False


def test_audit_sample_requires_eligible_offers_unless_internal_scope_is_requested(tmp_path):
    database = open_database(tmp_path / "catalog.sqlite")
    add_offer(database, "reserved", 1, title='=HYPERLINK("https://bad.example")')
    add_offer(database, "reserved", 2)
    output = tmp_path / "audits" / "sample.csv"

    with pytest.raises(ValueError, match="no offers"):
        write_audit_sample(database, {}, "jackets", "reserved", output, now=NOW)
    assert not output.exists()

    assert (
        write_audit_sample(database, {}, "jackets", "reserved", output, all_stored=True, now=NOW)
        == 2
    )
    with output.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert {row["source_product_id"] for row in rows} == {"1", "2"}
    assert all(row["public_eligible"] == "no" for row in rows)
    assert all(row["price_verified"] == "" for row in rows)
    assert all(row["reviewer"] == "" and row["reviewed_at"] == "" for row in rows)
    assert next(row for row in rows if row["source_product_id"] == "1")["title"].startswith("'=")
    with pytest.raises(FileExistsError):
        write_audit_sample(database, {}, "jackets", "reserved", output, all_stored=True)
    with output.open(newline="", encoding="utf-8") as stream:
        assert list(csv.DictReader(stream)) == rows

    eligible_output = tmp_path / "audits" / "eligible.csv"
    policies = {("reserved", "HU"): approved("reserved")}
    assert (
        write_audit_sample(database, policies, "jackets", "reserved", eligible_output, now=NOW) == 2
    )
    with eligible_output.open(newline="", encoding="utf-8") as stream:
        assert all(row["public_eligible"] == "yes" for row in csv.DictReader(stream))


def test_malformed_legacy_image_link_does_not_break_coverage_report(tmp_path):
    database = open_database(tmp_path / "catalog.sqlite")
    add_offer(database, "reserved", 1)
    StoredOffer.objects.using(database.alias).update(image_urls='["https://["]')

    report = readiness_report(
        database, {("reserved", "HU"): approved("reserved")}, "jackets", now=NOW
    )
    source = next(item for item in report["sources"] if item["source_key"] == "reserved")
    assert source["public_eligible_offers"] == 1
    assert source["image_candidates"] == 0

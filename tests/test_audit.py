"""Completed reviews are evidence only while their offers and checks remain current."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fashion_finder_data.audit import audit_report
from fashion_finder_data.readiness import write_audit_sample
from fashion_finder_data.schema import RetailerOffer
from fashion_finder_data.source_policy import SourcePolicy
from fashion_finder_data.storage import open_database, upsert_offer

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


def add_offer(database, source, number, *, price=9995, checked=NOW):
    url = f"https://www.{source}.com/hu/hu/jacket-{number}"
    upsert_offer(
        database,
        RetailerOffer(
            source_key=source,
            source_product_id=str(number),
            source_url=url,
            canonical_url=url,
            title=f"Jacket {number}",
            category="jackets",
            price_amount=Decimal(price),
            currency="HUF",
            size_options=["M"],
            source_fetched_at=checked,
        ),
    )


def policy(source):
    return SourcePolicy(
        source_key=source,
        approval_status="approved",
        evidence_ref="private evidence",
        allow_public_listing=True,
        freshness_hours=24,
    )


def complete_review(path):
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
        fields = list(rows[0])
    for row in rows:
        row.update(
            reviewer="local reviewer",
            reviewed_at=NOW.isoformat(),
            price_verified="yes",
            category_verified="yes",
            hungarian_link_verified="yes",
            availability_and_size_verified="yes",
        )
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return rows, fields


def save_rows(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_two_source_review_gate_requires_30_current_verified_offers_each(tmp_path):
    database = open_database(tmp_path / "catalog.sqlite")
    policies = {("reserved", "HU"): policy("reserved"), ("zara", "HU"): policy("zara")}
    paths = []
    for source in ("reserved", "zara"):
        for number in range(30):
            add_offer(database, source, number)
        path = tmp_path / f"{source}.csv"
        assert write_audit_sample(database, policies, "jackets", source, path, now=NOW) == 30
        complete_review(path)
        paths.append(path)

    report = audit_report(database, policies, "jackets", paths, now=NOW)
    assert report["two_source_recorded_audit_pass"] is True
    assert [source["passed_offers"] for source in report["sources"]] == [30, 30]
    assert report["live_product_pages_checked_by_tool"] is False
    assert "private evidence" not in str(report)

    rows, fields = complete_review(paths[1])
    rows[0]["price_verified"] = "no"
    save_rows(paths[1], rows, fields)
    report = audit_report(database, policies, "jackets", paths, now=NOW)
    assert report["two_source_recorded_audit_pass"] is False
    assert report["sources"][1]["passed_offers"] == 29
    assert report["sources"][1]["issue_counts"]["review_rejected"] == 1


def test_changed_offer_expired_review_and_revocation_fail_closed(tmp_path):
    database = open_database(tmp_path / "catalog.sqlite")
    add_offer(database, "reserved", 1)
    policies = {("reserved", "HU"): policy("reserved")}
    path = tmp_path / "reserved.csv"
    write_audit_sample(database, policies, "jackets", "reserved", path, now=NOW)
    complete_review(path)
    assert (
        audit_report(database, policies, "jackets", [path], now=NOW)["sources"][0]["passed_offers"]
        == 1
    )

    add_offer(database, "reserved", 1, price=12000)
    changed = audit_report(database, policies, "jackets", [path], now=NOW)["sources"][0]
    assert changed["passed_offers"] == 0
    assert changed["issue_counts"]["catalogue_changed_since_sample"] == 1

    add_offer(database, "reserved", 1, checked=NOW + timedelta(hours=23))
    expired = audit_report(database, policies, "jackets", [path], now=NOW + timedelta(hours=25))[
        "sources"
    ][0]
    assert expired["issue_counts"]["review_expired"] == 1
    assert "not_currently_eligible" not in expired["issue_counts"]
    revoked = audit_report(database, {}, "jackets", [path], now=NOW)["sources"][0]
    assert revoked["issue_counts"]["not_currently_eligible"] == 1


def test_invalid_verdict_and_duplicate_offer_are_rejected(tmp_path):
    database = open_database(tmp_path / "catalog.sqlite")
    add_offer(database, "reserved", 1)
    add_offer(database, "reserved", "+special")
    policies = {("reserved", "HU"): policy("reserved")}
    path = tmp_path / "reserved.csv"
    write_audit_sample(database, policies, "jackets", "reserved", path, now=NOW)
    rows, fields = complete_review(path)
    assert (
        audit_report(database, policies, "jackets", [path], now=NOW)["sources"][0]["passed_offers"]
        == 2
    )
    fields.remove("offer_id")
    rows = [{field: row[field] for field in fields} for row in rows]
    save_rows(path, rows, fields)
    assert (
        audit_report(database, policies, "jackets", [path], now=NOW)["sources"][0]["passed_offers"]
        == 2
    )
    rows[0]["price_verified"] = "maybe"
    save_rows(path, rows, fields)
    with pytest.raises(ValueError, match="must be yes, no, or blank"):
        audit_report(database, policies, "jackets", [path], now=NOW)

    rows[0]["price_verified"] = "yes"
    save_rows(path, rows * 2, fields)
    with pytest.raises(ValueError, match="duplicate offer URL"):
        audit_report(database, policies, "jackets", [path], now=NOW)

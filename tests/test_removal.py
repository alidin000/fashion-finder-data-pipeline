"""Stale listings require direct review and never imply an unavailable state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fashion_finder_data.catalogue.models import RetailerOffer
from fashion_finder_data.pipeline import ingest_records
from fashion_finder_data.removal import recheck_report
from fashion_finder_data.schema import RawProductRecord
from fashion_finder_data.storage import open_database


def _record(sku: str, when: datetime) -> RawProductRecord:
    url = f"https://www.reserved.com/hu/hu/example-coat-{sku.lower()}-45c"
    return RawProductRecord(
        source_key="reserved",
        source_url=url,
        fetched_at=when,
        product={
            "url": url,
            "sku": sku,
            "name": "Example coat",
            "price": "24995",
            "currency": "HUF",
        },
    )


def test_recheck_lists_only_stale_or_invalid_and_preserves_stock(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "offers.sqlite")
    now = datetime(2026, 10, 12, tzinfo=UTC)
    ingest_records(
        engine,
        tmp_path,
        "reserved",
        [_record("OLD", now - timedelta(days=9)), _record("FRESH", now)],
    )
    report = recheck_report(engine, as_of=now, stale_after_days=7)
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["source_product_id"] == "OLD"
    assert report["candidates"][0]["action"] == "direct_recheck"
    assert report["policy"]["reserved"]["http_403_429_5xx"] == "recheck"
    states = list(
        RetailerOffer.objects.using(engine.alias).values_list("status", "availability_status")
    )
    assert states == [("active", "unknown"), ("active", "unknown")]


def test_recheck_refuses_bad_url_as_direct_request(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "offers.sqlite")
    now = datetime(2026, 10, 12, tzinfo=UTC)
    ingest_records(engine, tmp_path, "reserved", [_record("BAD", now)])
    RetailerOffer.objects.using(engine.alias).update(canonical_url="https://example.com/wrong")
    report = recheck_report(engine, as_of=now)
    assert report["candidates"][0]["product_url"] is None
    assert report["candidates"][0]["action"] == "manual_review"


def test_recheck_flags_future_timestamp_for_manual_review(tmp_path: Path) -> None:
    engine = open_database(tmp_path / "offers.sqlite")
    now = datetime(2026, 10, 12, tzinfo=UTC)
    ingest_records(engine, tmp_path, "reserved", [_record("FUTURE", now + timedelta(days=1))])
    report = recheck_report(engine, as_of=now)
    assert report["candidates"][0]["reason"] == "future_timestamp"
    assert report["candidates"][0]["action"] == "manual_review"

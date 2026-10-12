"""Scheduled feeds must never make unchanged or old data look newly checked."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fashion_finder_data.catalogue.models import (
    CollectionRun,
    PriceObservation,
    RetailerOffer,
)
from fashion_finder_data.refresh import refresh_once
from fashion_finder_data.storage import open_database

URL = "https://www.reserved.com/hu/hu/scheduled-jacket"


def write_policy(path, *, approved=True):
    path.write_text(
        json.dumps(
            [
                {
                    "source_key": "reserved",
                    "approval_status": "approved" if approved else "pending",
                    "evidence_ref": "private approval" if approved else None,
                    "access_method": "feed-csv",
                    "allow_public_listing": approved,
                }
            ]
        ),
        encoding="utf-8",
    )


def write_feed(path, *, observed_at, price="9995", timestamp_column=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "product_url,name,price,currency,sku"
    row = f"{URL},Scheduled jacket,{price},HUF,scheduled"
    if timestamp_column:
        header += ",observed_at"
        row += f",{observed_at}"
    path.write_text(header + "\n" + row + "\n", encoding="utf-8")


def test_refresh_imports_only_changed_approved_timestamped_feeds(tmp_path):
    database = open_database(tmp_path / "fashion_finder.sqlite")
    feed_dir = tmp_path / "feeds"
    feed = feed_dir / "reserved.csv"
    policy = tmp_path / "source-policies.json"
    state = tmp_path / "worker-state.json"
    observed = datetime.now(UTC) - timedelta(hours=2)
    write_policy(policy)
    write_feed(feed, observed_at=observed.isoformat())

    first = refresh_once(database, tmp_path, feed_dir, policy, state)
    assert first["sources"][0]["status"] == "success"
    offer = RetailerOffer.objects.using(database.alias).get()
    assert offer.source_fetched_at == observed.isoformat()
    assert CollectionRun.objects.using(database.alias).count() == 1

    second = refresh_once(database, tmp_path, feed_dir, policy, state)
    assert second["status"] == "idle"
    assert second["sources"][0]["status"] == "unchanged"
    assert CollectionRun.objects.using(database.alias).count() == 1
    assert (
        RetailerOffer.objects.using(database.alias).get().source_fetched_at == observed.isoformat()
    )

    updated = datetime.now(UTC)
    write_feed(feed, observed_at=updated.isoformat(), price="12000")
    third = refresh_once(database, tmp_path, feed_dir, policy, state)
    assert third["status"] == "success"
    assert RetailerOffer.objects.using(database.alias).get().price_amount == "12000"
    assert PriceObservation.objects.using(database.alias).count() == 2


def test_refresh_blocks_unapproved_and_rejects_missing_or_future_observation(tmp_path):
    database = open_database(tmp_path / "fashion_finder.sqlite")
    feed_dir = tmp_path / "feeds"
    feed = feed_dir / "reserved.csv"
    policy = tmp_path / "source-policies.json"
    state = tmp_path / "worker-state.json"
    write_feed(feed, observed_at="", timestamp_column=False)
    write_policy(policy, approved=False)

    blocked = refresh_once(database, tmp_path, feed_dir, policy, state)
    assert blocked["sources"][0]["status"] == "blocked_by_policy"
    assert RetailerOffer.objects.using(database.alias).count() == 0

    write_policy(policy)
    missing = refresh_once(database, tmp_path, feed_dir, policy, state)
    assert missing["sources"][0]["status"] == "failed"
    assert not state.exists()

    write_feed(feed, observed_at=(datetime.now(UTC) + timedelta(days=1)).isoformat())
    future = refresh_once(database, tmp_path, feed_dir, policy, state)
    assert future["sources"][0]["status"] == "failed"
    assert RetailerOffer.objects.using(database.alias).count() == 0
    assert not state.exists()

"""Refresh runs: planning, outcome mapping, reporting and a replayed round trip."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from fashion_finder_data.collectors import CrawlState
from fashion_finder_data.runner import (
    ApiBackend,
    _batches,
    markdown,
    plan_refresh,
    refresh_items,
)

REPLAY = Path(__file__).parent / "fixtures" / "replay"
LISTING = "https://www.reserved.com/hu/hu/noi/ruhazat/kabatok"


def due(offer_id: int, url: str, kind: str = "product", check: str | None = None) -> dict:
    return {
        "id": offer_id,
        "source_key": "reserved",
        "product_url": url,
        "check_url": check or url,
        "check_kind": kind,
        "sku": f"SKU-{offer_id}",
    }


def test_plan_groups_offers_by_check_url_within_the_request_budget() -> None:
    offers = [
        due(1, "https://www.reserved.com/hu/hu/a-111aa-11x", "listing", LISTING),
        due(2, "https://www.reserved.com/hu/hu/b-222bb-22x", "listing", LISTING),
        due(3, "https://www.reserved.com/hu/hu/c-333cc-33x"),
        due(4, "https://www.reserved.com/hu/hu/d-444dd-44x"),
    ]
    plan, chosen = plan_refresh(offers, requests_available=3)
    assert [(entry.url, entry.kind) for entry in plan] == [
        (LISTING, "listing"),
        ("https://www.reserved.com/hu/hu/c-333cc-33x", "product"),
    ]  # the listing costs two requests, so the fourth offer waits for the next run
    assert [offer["id"] for offer in chosen[LISTING]] == [1, 2]


def test_outcomes_become_ingest_items_and_unattempted_offers_stay_due() -> None:
    state = CrawlState()
    gone = "https://www.reserved.com/hu/hu/gone-111aa-11x"
    broken = "https://www.reserved.com/hu/hu/broken-222bb-22x"
    same = "https://www.reserved.com/hu/hu/same-333cc-33x"
    late = "https://www.reserved.com/hu/hu/late-444dd-44x"
    listed = "https://www.reserved.com/hu/hu/listed-555ee-55x"
    absent = "https://www.reserved.com/hu/hu/absent-666ff-66x"
    state.outcome("reserved", gone, "gone", reason="HTTP 404", status=404)
    state.outcome("reserved", broken, "failed", reason="HTTP 500", status=500)
    state.outcome("reserved", same, "not_modified", status=304)
    state.outcome("reserved", LISTING, "ok", kind="listing")
    state.complete_listings["reserved"] = {LISTING}
    state.listing_seen["reserved"] = {LISTING: {listed}}
    groups = {
        "reserved": {
            gone: [due(1, gone)],
            broken: [due(2, broken)],
            same: [due(3, same)],
            late: [due(4, late)],
            LISTING: [due(5, listed, "listing", LISTING), due(6, absent, "listing", LISTING)],
        }
    }
    items, reasons = refresh_items(state, groups)
    by_offer = {item["offer_id"]: item for item in items}
    assert by_offer[1]["type"] == "failure" and by_offer[1]["gone"] is True
    assert by_offer[2]["type"] == "failure" and by_offer[2]["gone"] is False
    assert by_offer[3]["type"] == "not_modified"
    assert 4 not in by_offer and reasons["not attempted"] == 1
    assert 5 not in by_offer  # seen in the listing; its observation carries the update
    assert by_offer[6]["type"] == "missing"


def test_markdown_summary_shows_failures_and_stretched_intervals() -> None:
    report = {
        "run_id": "refresh-x",
        "backend": "api",
        "started_at": "2026-10-24T08:17:00+00:00",
        "duration_seconds": 61.0,
        "sources": {
            "reserved": {
                "due_at_start": 10,
                "checked_offers": 8,
                "results": {"updated": 2, "unchanged": 5},
                "failures_by_reason": {"HTTP 404": 1},
                "due_remaining": 2,
                "requests": 9,
                "latency_ms_p50": 300,
                "latency_ms_p95": 900,
                "effective_interval_hours": 28.4,
                "stretched": True,
                "reason": "Crawl-delay 10 s",
                "stopped": None,
                "stale_over_1_5x_interval": 0,
            }
        },
    }
    text = markdown(report, "Fashion Finder refresh")
    assert "| reserved | 10 | 8 | 2/5/0 | 1 | 2 | 9 | 300/900 | 28.4 h (stretched) |" in text
    assert "HTTP 404: 1" in text and "interval stretched: Crawl-delay 10 s" in text


def test_api_mode_refuses_plain_http_and_missing_tokens() -> None:
    with pytest.raises(ValueError, match="https"):
        ApiBackend("http://api.example.org", "t" * 40)
    with pytest.raises(ValueError, match="FASHION_FINDER_API_TOKEN"):
        ApiBackend("https://api.example.org", "")
    assert ApiBackend("http://127.0.0.1:8204", "t" * 40).base == "http://127.0.0.1:8204"


def test_batches_respect_item_and_byte_limits() -> None:
    small = [{"type": "x"}] * 1200
    assert [len(batch) for batch in _batches(small)] == [500, 500, 200]
    big = [{"pad": "x" * 1_000_000}] * 7
    assert [len(batch) for batch in _batches(big)] == [3, 3, 1]


def _cli(*args: str, env: dict) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "fashion_finder_data.cli", *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert completed.returncode in (0, 1), completed.stderr[-3000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_replayed_discovery_then_refresh_round_trip(tmp_path) -> None:
    """No network: recorded responses stand in for the shop, the real crawler runs."""
    data = tmp_path / "data"
    summary = tmp_path / "summary.md"
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary)}
    env.pop("FASHION_FINDER_API_URL", None)
    common = ["--delay", "0"]
    discovered = _cli(
        "--data-dir", str(data), "collect", "reserved", "--limit", "50",
        "--replay-dir", str(REPLAY / "lpp-v1"), *common, env=env,
    )  # fmt: skip
    assert discovered["records_valid"] == 3 and discovered["offers"]["new"] == 3
    with sqlite3.connect(data / "fashion_finder.sqlite") as connection:
        connection.execute("UPDATE retailer_offer SET next_refresh_at='2026-01-01 00:00:00'")
    report = _cli(
        "--data-dir", str(data), "refresh", "--source", "reserved",
        "--replay-dir", str(REPLAY / "lpp-v2"), *common, env=env,
    )  # fmt: skip
    row = report["sources"]["reserved"]
    assert row["due_at_start"] == 3 and row["checked_offers"] == 3
    assert Counter(row["results"]) == Counter({"unchanged": 1, "updated": 1, "recorded": 1})
    assert row["failures_by_reason"] == {"missing from complete listing": 1}
    assert row["cycle_complete"] is True
    with sqlite3.connect(data / "fashion_finder.sqlite") as connection:
        prices = dict(
            connection.execute("SELECT source_product_id, price_amount FROM retailer_offer")
        )
        errors = dict(
            connection.execute("SELECT source_product_id, last_error FROM retailer_offer")
        )
    assert prices["202BB-80X"] == "19995"
    assert errors["303CC-01X"] == "missing from complete listing"
    assert "| reserved | 3 | 3 | 1/1/0 | 1 | 0 |" in summary.read_text(encoding="utf-8")

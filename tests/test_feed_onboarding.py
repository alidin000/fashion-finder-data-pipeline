"""Feed-only source onboarding and publication boundaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from django.test import Client

from fashion_finder_data.api.views import api_context
from fashion_finder_data.cli import main
from fashion_finder_data.feed import validate_feed
from fashion_finder_data.refresh import refresh_once
from fashion_finder_data.sources import CRAWL_SOURCES, SOURCES, allowed_url
from fashion_finder_data.storage import open_database


@pytest.mark.parametrize(
    ("source", "url"),
    [
        ("aboutyou", "https://www.aboutyou.hu/p/label/jacket-12345"),
        ("answear", "https://answear.hu/p/label/jacket-12345"),
    ],
)
def test_candidate_feed_requires_policy_before_public_search(tmp_path, source, url):
    observed = datetime.now(UTC).isoformat()
    feed_dir = tmp_path / "feeds"
    feed_dir.mkdir()
    feed = feed_dir / f"{source}.csv"
    feed.write_text(
        "product_url,name,price,currency,category,observed_at\n"
        f"{url},Blue jacket,12990,HUF,jackets,{observed}\n",
        encoding="utf-8",
    )
    assert source not in CRAWL_SOURCES
    assert SOURCES[source].category_url is None
    assert validate_feed(feed, source)["status"] == "valid"

    database_path = tmp_path / "catalog.sqlite"
    database = open_database(database_path)
    state = tmp_path / "worker-state.json"
    blocked = refresh_once(database, tmp_path, feed_dir, None, state)
    assert blocked["sources"] == [{"source_key": source, "status": "blocked_by_policy"}]
    assert not state.exists()

    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            [
                {
                    "source_key": source,
                    "approval_status": "approved",
                    "evidence_ref": "synthetic test approval",
                    "access_method": "feed-csv",
                    "allow_public_listing": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    imported = refresh_once(database, tmp_path, feed_dir, policy, state)
    assert imported["sources"][0]["status"] == "success"
    assert imported["sources"][0]["records_valid"] == 1
    with api_context(database, policy):
        response = Client().get("/api/v1/search", {"source": source})
    assert response.status_code == 200
    assert [item["source_key"] for item in response.json()["items"]] == [source]


@pytest.mark.parametrize("source", ["aboutyou", "answear"])
def test_validate_feed_rejects_bad_rows_without_creating_data(tmp_path, capsys, source):
    feed = tmp_path / "candidate.csv"
    observed = datetime.now(UTC).isoformat()
    feed.write_text(
        "product_url,name,price,currency,observed_at\n"
        f"https://www.{source}.hu/p/label/jacket-1,Good,9900,HUF,{observed}\n"
        f"https://evil.example/p/label/jacket-2,Bad host,9900,HUF,{observed}\n"
        f"https://www.{source}.hu/p/label/jacket-3,Bad price,wrong,HUF,{observed}\n",
        encoding="utf-8",
    )
    result = validate_feed(feed, source)
    assert result["status"] == "invalid"
    assert result["records_valid"] == 1
    assert result["records_rejected"] == 2
    assert main(["--data-dir", str(tmp_path / "data"), "validate-feed", source, str(feed)]) == 1
    assert json.loads(capsys.readouterr().out)["records_rejected"] == 2
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("source", ["aboutyou", "answear"])
def test_candidate_urls_require_hungarian_product_page(source):
    configured = SOURCES[source]
    assert allowed_url(configured, f"https://www.{configured.host}/p/brand/coat-1")
    for url in (
        f"http://www.{configured.host}/p/brand/coat-1",
        f"https://www.{configured.host}/pl/p/brand/coat-1",
        f"https://www.{configured.host}:444/p/brand/coat-1",
        f"https://user:pass@www.{configured.host}/p/brand/coat-1",
        f"https://www.{configured.host}.evil.example/p/brand/coat-1",
        f"https://other.{configured.host}/p/brand/coat-1",
    ):
        assert not allowed_url(configured, url)

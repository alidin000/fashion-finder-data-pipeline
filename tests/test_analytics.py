"""Anonymous counters reject raw inputs and expire old aggregate rows."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from django.test import Client

from fashion_finder_data.analytics import metric_report, prune_metrics, record_metric
from fashion_finder_data.api.views import api_context
from fashion_finder_data.catalogue.models import DailyMetric
from fashion_finder_data.source_policy import SourcePolicy
from fashion_finder_data.storage import open_database


def approved():
    return {
        ("reserved", "HU"): SourcePolicy(
            source_key="reserved",
            approval_status="approved",
            evidence_ref="private approval",
            allow_public_listing=True,
        )
    }


def test_event_api_is_opt_in_and_never_accepts_raw_queries(tmp_path, monkeypatch):
    database = open_database(tmp_path / "catalog.sqlite")
    policy_path = tmp_path / "policies.json"
    policy_path.write_text(json.dumps([approved()[("reserved", "HU")].model_dump()]))
    client = Client()
    with api_context(database, policy_path):
        assert client.get("/api/v1/health").json()["analytics_enabled"] is False
        assert (
            client.post("/api/v1/events", data="{}", content_type="application/json").status_code
            == 404
        )
        monkeypatch.setenv("FASHION_FINDER_ANALYTICS_ENABLED", "1")
        assert client.get("/api/v1/health").json()["analytics_enabled"] is True
        search = {
            "event_type": "search",
            "search_type": "text",
            "filtered": True,
            "result_count": 3,
        }
        for _ in range(2):
            assert (
                client.post(
                    "/api/v1/events", data=json.dumps(search), content_type="application/json"
                ).status_code
                == 201
            )
        click = {"event_type": "click", "search_type": "text", "source_key": "reserved"}
        assert (
            client.post(
                "/api/v1/events", data=json.dumps(click), content_type="application/json"
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/api/v1/events",
                data=json.dumps({**search, "query": "private query"}),
                content_type="application/json",
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/v1/events",
                data=json.dumps({**click, "source_key": "zara"}),
                content_type="application/json",
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/v1/events", data="x" * 1025, content_type="application/json"
            ).status_code
            == 413
        )

    report = metric_report(database)
    assert sum(row["total"] for row in report["counters"]) == 3
    assert {row["event_type"] for row in report["counters"]} == {"search", "click"}
    assert "private query" not in str(report)
    assert DailyMetric.objects.using(database.alias).count() == 2


def test_daily_aggregate_retention_keeps_only_last_30_days(tmp_path):
    database = open_database(tmp_path / "catalog.sqlite")
    now = datetime(2026, 10, 13, tzinfo=UTC)
    payload = {"event_type": "search", "search_type": "photo", "result_count": 0}
    record_metric(database, payload, {}, now=now - timedelta(days=30))
    record_metric(database, payload, {}, now=now - timedelta(days=29))
    record_metric(database, payload, {}, now=now)

    assert prune_metrics(database, now=now) == 1
    report = metric_report(database, days=30, now=now)
    assert sum(row["total"] for row in report["counters"]) == 2
    assert all("query" not in row for row in report["counters"])

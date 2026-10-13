"""Synthetic local demo stays separate from retailer release evidence."""

from __future__ import annotations

import json

import pytest
from django.test import Client

from fashion_finder_data.api.views import api_context
from fashion_finder_data.demo import DEMO_PRODUCTS, seed_demo
from fashion_finder_data.evaluation import evaluate_tasks
from fashion_finder_data.readiness import readiness_report, write_audit_sample
from fashion_finder_data.source_policy import load_policies
from fashion_finder_data.storage import offer_rows, open_database


def test_demo_search_is_explicit_and_cannot_pass_release_gates(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    monkeypatch.setenv("FASHION_FINDER_DEMO_MODE", "1")
    first = seed_demo(root)
    second = seed_demo(root)
    assert first["records_valid"] == second["records_valid"] == len(DEMO_PRODUCTS)
    database = open_database(root / "fashion_finder.sqlite")
    assert len(offer_rows(database)) == len(DEMO_PRODUCTS)
    policy = root / "source-policies.json"
    policies = load_policies(policy)

    with api_context(database, policy):
        client = Client()
        assert client.get("/api/v1/health").json()["demo_mode"] is True
        search = client.get("/api/v1/search", {"category": "jackets", "max_price": "15000"})
    assert search.status_code == 200
    offers = search.json()["items"]
    assert len(offers) == 3
    assert all(item["source_key"] == "demo" for item in offers)
    assert all(
        item["source_url"].startswith("https://fashion-finder-demo.invalid/") for item in offers
    )
    assert all(item["image_urls"] == [] for item in offers)

    readiness = readiness_report(database, policies, "jackets")
    assert "demo" not in {item["source_key"] for item in readiness["sources"]}
    assert readiness["text_coverage_pass"] is False
    with pytest.raises(ValueError, match="demo offers cannot be used"):
        write_audit_sample(database, policies, "jackets", "demo", root / "audit.csv")

    tasks = root / "tasks.json"
    tasks.write_text(
        json.dumps(
            [
                {
                    "task_id": "synthetic-001",
                    "consent_ref": "synthetic-ref",
                    "input_ref": "synthetic-input",
                    "category": "jackets",
                    "text_result_ids": [item["id"] for item in offers],
                    "text_useful_ids": [item["id"] for item in offers],
                    "text_latency_ms": 1,
                }
            ]
        ),
        encoding="utf-8",
    )
    evaluation = evaluate_tasks(database, policies, "jackets", tasks)
    assert evaluation["task_outcomes"][0]["current_useful_text_options"] == 0

    monkeypatch.delenv("FASHION_FINDER_DEMO_MODE")
    with api_context(database, policy):
        assert Client().get("/api/v1/search").json()["items"] == []
        assert Client().get("/api/v1/health").json()["demo_mode"] is False


def test_seed_refuses_real_catalogue_and_requires_demo_flag(tmp_path, monkeypatch):
    root = tmp_path / "catalogue"
    open_database(root / "fashion_finder.sqlite")
    monkeypatch.setenv("FASHION_FINDER_DEMO_MODE", "1")
    with pytest.raises(ValueError, match="existing catalogue"):
        seed_demo(root)
    assert not (root / "source-policies.json").exists()

    monkeypatch.delenv("FASHION_FINDER_DEMO_MODE")
    with pytest.raises(ValueError, match="FASHION_FINDER_DEMO_MODE"):
        seed_demo(tmp_path / "fresh-demo")

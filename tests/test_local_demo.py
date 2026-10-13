"""Synthetic local demo stays separate from retailer release evidence."""

from __future__ import annotations

import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from fashion_finder_data.api.views import api_context
from fashion_finder_data.demo import DEMO_PRODUCTS, LEGACY_DEMO_POLICY, MARKER, seed_demo
from fashion_finder_data.demo_images import demo_image_bytes
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
        health = Client().get("/api/v1/health").json()
        assert health["demo_mode"] is False
        assert health["image_search"] == "unavailable"
        assert Client().get("/api/v1/demo/images/demo-003").status_code == 404
        assert (
            Client()
            .post(
                "/api/v1/search/image",
                {
                    "file": SimpleUploadedFile(
                        "jacket.jpg", demo_image_bytes("demo-003"), content_type="image/jpeg"
                    )
                },
            )
            .status_code
            == 503
        )


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


def test_demo_photo_search_ranks_same_image_and_honours_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("FASHION_FINDER_DEMO_MODE", "1")
    root = tmp_path / "demo"
    seed_demo(root)
    database = open_database(root / "fashion_finder.sqlite")
    policy = root / "source-policies.json"
    image = demo_image_bytes("demo-008")
    assert image is not None
    with api_context(database, policy):
        client = Client()
        assert client.get("/api/v1/health").json()["image_search"] == "available"
        response = client.post(
            "/api/v1/search/image",
            {
                "file": SimpleUploadedFile("dress.jpg", image, content_type="image/jpeg"),
            },
        )
        filtered = client.post(
            "/api/v1/search/image",
            {
                "file": SimpleUploadedFile("dress.jpg", image, content_type="image/jpeg"),
                "category": "dresses",
                "max_price": "15000",
            },
        )
        sample = client.get("/api/v1/demo/images/demo-008")
        assert sample.status_code == 200
        assert sample["Content-Type"] == "image/jpeg"
        assert client.get("/api/v1/demo/images/unknown").status_code == 404
        oversized_filter = client.post(
            "/api/v1/search/image",
            {
                "file": SimpleUploadedFile("dress.jpg", image, content_type="image/jpeg"),
                "category": "x" * 81,
            },
        )
        assert oversized_filter.status_code == 400
    assert response.status_code == 200
    assert response.json()["items"][0]["source_product_id"] == "demo-008"
    assert [item["source_product_id"] for item in filtered.json()["items"]] == ["demo-008"]
    assert response.json()["items"][0]["match_type"] == "style_inspiration"
    assert response.json()["items"][0]["image_urls"] == ["/api/v1/demo/images/demo-008"]


def test_existing_generated_demo_policy_upgrades_without_replacing_custom_policy(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FASHION_FINDER_DEMO_MODE", "1")
    root = tmp_path / "demo"
    root.mkdir()
    (root / ".demo-catalogue").write_text(MARKER, encoding="utf-8")
    policy = root / "source-policies.json"
    policy.write_text(json.dumps(LEGACY_DEMO_POLICY, indent=2) + "\n", encoding="utf-8")
    seed_demo(root)
    assert load_policies(policy)[("demo", "HU")].allow_image_processing is True
    policy.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="existing source policy"):
        seed_demo(root)

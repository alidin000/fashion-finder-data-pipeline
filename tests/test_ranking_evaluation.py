"""Rankings are replayed from private tasks, not accepted as caller assertions."""

from __future__ import annotations

import json
import math
from decimal import Decimal

import pytest

from fashion_finder_data.demo_images import demo_image_bytes
from fashion_finder_data.image_index import build_image_index
from fashion_finder_data.ranking_evaluation import evaluate_rankings, ranking_metrics
from fashion_finder_data.schema import RetailerOffer, utc_now
from fashion_finder_data.source_policy import SourcePolicy
from fashion_finder_data.storage import offer_rows, open_database, upsert_offer


def _catalogue(tmp_path):
    database = open_database(tmp_path / "catalog.sqlite")
    for source, sku, price in (
        ("reserved", "red", 9995),
        ("reserved", "brown", 11995),
        ("zara", "third", 12995),
        ("zara", "expensive", 29995),
    ):
        url = f"https://www.{source}.com/hu/hu/jacket-{sku}"
        upsert_offer(
            database,
            RetailerOffer(
                source_key=source,
                source_product_id=sku,
                source_url=url,
                canonical_url=url,
                title=f"Jacket {sku}",
                category="jackets",
                price_amount=Decimal(price),
                currency="HUF",
                image_urls=[f"https://www.{source}.com/hu/hu/images/{sku}.jpg"],
                source_fetched_at=utc_now(),
            ),
        )
    policies = {
        (source, "HU"): SourcePolicy(
            source_key=source,
            approval_status="approved",
            evidence_ref="synthetic test fixture",
            allow_public_listing=True,
            allow_image_display=True,
            allow_image_processing=True,
        )
        for source in ("reserved", "zara")
    }
    rows = offer_rows(database)
    ids = {row["source_product_id"]: row["id"] for row in rows}
    photo = demo_image_bytes("demo-008")
    assert photo is not None
    (tmp_path / "photo.jpg").write_bytes(photo)
    manifest = [
        {
            "offer_id": row["id"],
            "image_url": json.loads(row["image_urls"])[0],
            "image_path": "photo.jpg",
        }
        for row in rows
        if row["source_product_id"] != "expensive"
    ]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    build_image_index(database, policies, manifest_path)
    return database, policies, ids


def _task(ids, **changes):
    task = {
        "task_id": "one",
        "consent_ref": "private-consent-one",
        "input_ref": "private-input-one",
        "category": "jackets",
        "query": "Jacket",
        "max_price": "15000",
        "photo_path": "photo.jpg",
        "relevant_offer_ids": [ids["red"], ids["brown"], ids["third"]],
    }
    task.update(changes)
    return task


def test_replay_measures_text_and_photo_rankings_with_same_filters(tmp_path):
    database, policies, ids = _catalogue(tmp_path)
    task_path = tmp_path / "tasks.json"
    task_path.write_text(json.dumps([_task(ids)]), encoding="utf-8")

    report = evaluate_rankings(database, policies, "jackets", task_path)
    outcome = report["task_outcomes"][0]
    assert report["ranking_computed_by_tool"] is True
    assert report["text_task_gate_met"] is False  # One task cannot satisfy the 30-task gate.
    assert report["photo_tasks_evaluated"] == 1
    assert outcome["text_success"] is True
    assert outcome["text_useful_sources"] == 2
    assert (
        set(outcome["text_result_ids"])
        == set(outcome["photo_result_ids"])
        == {ids["red"], ids["brown"], ids["third"]}
    )
    assert ids["expensive"] not in outcome["text_result_ids"]
    assert outcome["photo_top5_relevant"] == outcome["text_top5_relevant"] == 3
    assert "private-consent" not in str(report)
    assert "photo.jpg" not in str(report)
    assert "Jacket" not in str(report)

    policies.pop(("zara", "HU"))
    without_zara = evaluate_rankings(database, policies, "jackets", task_path)
    reduced = without_zara["task_outcomes"][0]
    assert reduced["text_success"] is False
    assert ids["third"] not in reduced["text_result_ids"]
    assert ids["third"] not in reduced["photo_result_ids"]


def test_replay_rejects_unapproved_or_escaping_photos(tmp_path):
    database, policies, ids = _catalogue(tmp_path)
    task_path = tmp_path / "tasks.json"
    task_path.write_text(json.dumps([_task(ids, photo_path="../outside.jpg")]), encoding="utf-8")
    with pytest.raises(ValueError, match="outside the task directory"):
        evaluate_rankings(database, policies, "jackets", task_path)

    task_path.write_text(json.dumps([_task(ids)]), encoding="utf-8")
    denied = {
        key: policy.model_copy(update={"allow_image_processing": False})
        for key, policy in policies.items()
    }
    with pytest.raises(ValueError, match="no current permitted image index"):
        evaluate_rankings(database, denied, "jackets", task_path)


def test_replay_validation_error_does_not_echo_private_input(tmp_path):
    database, policies, ids = _catalogue(tmp_path)
    task_path = tmp_path / "tasks.json"
    private_query = "private-shopping-phrase-" + "x" * 200
    task_path.write_text(json.dumps([_task(ids, query=private_query)]), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid ranking task data") as failure:
        evaluate_rankings(database, policies, "jackets", task_path)
    assert private_query not in str(failure.value)


def test_replay_cannot_count_demo_mode_as_release_evidence(tmp_path, monkeypatch):
    database, policies, ids = _catalogue(tmp_path)
    task_path = tmp_path / "tasks.json"
    task_path.write_text(json.dumps([_task(ids)]), encoding="utf-8")
    monkeypatch.setenv("FASHION_FINDER_DEMO_MODE", "1")
    with pytest.raises(ValueError, match="cannot run in demo mode"):
        evaluate_rankings(database, policies, "jackets", task_path)


def test_ranking_metrics_reward_relevant_results_near_the_top() -> None:
    second = ranking_metrics([7, 3, 9], {3})
    assert second["hit_at_5"] == 1.0
    assert second["mrr_at_10"] == 0.5
    assert second["ndcg_at_10"] == pytest.approx(1 / math.log2(3))
    sixth = ranking_metrics([1, 2, 4, 5, 6, 3], {3})
    assert (sixth["hit_at_5"], sixth["mrr_at_10"]) == (0.0, pytest.approx(1 / 6))
    assert ranking_metrics([1, 2], {3}) == {"hit_at_5": 0.0, "mrr_at_10": 0.0, "ndcg_at_10": 0.0}

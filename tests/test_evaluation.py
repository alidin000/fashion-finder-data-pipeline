"""Task thresholds use current permitted offers and explicit human relevance labels."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fashion_finder_data.catalogue.models import RetailerOffer as StoredOffer
from fashion_finder_data.evaluation import evaluate_tasks
from fashion_finder_data.schema import RetailerOffer
from fashion_finder_data.source_policy import SourcePolicy
from fashion_finder_data.storage import open_database, upsert_offer

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


def add_offer(database, source, sku):
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
            price_amount=Decimal(9995),
            currency="HUF",
            image_urls=[f"https://www.{source}.com/image/{sku}.jpg"],
            source_fetched_at=NOW,
        ),
    )


def approved(source, *, images=True):
    return SourcePolicy(
        source_key=source,
        approval_status="approved",
        evidence_ref="private agreement",
        allow_public_listing=True,
        allow_image_display=images,
        allow_image_processing=images,
        freshness_hours=24,
    )


def write_tasks(path, offer_ids):
    tasks = []
    for number in range(30):
        tasks.append(
            {
                "task_id": f"task-{number}",
                "consent_ref": f"private-consent-{number}",
                "input_ref": f"private-input-{number}",
                "category": "jackets",
                "text_result_ids": offer_ids,
                "text_useful_ids": offer_ids if number < 24 else offer_ids[:2],
                "text_latency_ms": 100,
                "photo_result_ids": offer_ids,
                "photo_relevant_ids": offer_ids,
                "photo_latency_ms": 500,
            }
        )
    path.write_text(json.dumps(tasks), encoding="utf-8")
    return tasks


def test_task_gate_and_photo_comparison_use_current_approved_offers(tmp_path):
    database = open_database(tmp_path / "catalog.sqlite")
    for source, sku in (("reserved", "a"), ("reserved", "b"), ("zara", "c")):
        add_offer(database, source, sku)
    ids = list(
        StoredOffer.objects.using(database.alias).order_by("id").values_list("id", flat=True)
    )
    path = tmp_path / "tasks.json"
    write_tasks(path, ids)
    policies = {("reserved", "HU"): approved("reserved"), ("zara", "HU"): approved("zara")}

    report = evaluate_tasks(database, policies, "jackets", path, now=NOW)
    assert report["submitted_tasks"] == 30
    assert report["tasks_with_three_current_options_from_two_sources"] == 24
    assert report["text_task_gate_met"] is True
    assert report["photo_tasks_evaluated"] == 30
    assert report["photo_top5_relevant_mean"] == 3
    assert report["text_top5_relevant_mean_on_photo_tasks"] == 2.8
    assert report["consent_verified_by_tool"] is False
    assert "private-consent" not in str(report)

    policies[("zara", "HU")] = approved("zara", images=False)
    without_image_rights = evaluate_tasks(database, policies, "jackets", path, now=NOW)
    assert without_image_rights["text_task_gate_met"] is True
    assert without_image_rights["photo_tasks_evaluated"] == 0

    del policies[("zara", "HU")]
    revoked = evaluate_tasks(database, policies, "jackets", path, now=NOW)
    assert revoked["text_task_gate_met"] is False


def test_evaluation_rejects_untraceable_labels_and_raw_query_fields(tmp_path):
    database = open_database(tmp_path / "catalog.sqlite")
    path = tmp_path / "tasks.json"
    tasks = write_tasks(path, [1, 2, 3])
    tasks[0]["text_useful_ids"] = [4]
    path.write_text(json.dumps(tasks), encoding="utf-8")
    with pytest.raises(ValueError, match="text useful IDs"):
        evaluate_tasks(database, {}, "jackets", path, now=NOW)

    tasks[0]["text_useful_ids"] = [1]
    tasks[0]["query"] = "private shopper query"
    path.write_text(json.dumps(tasks), encoding="utf-8")
    with pytest.raises(ValueError, match="extra_forbidden"):
        evaluate_tasks(database, {}, "jackets", path, now=NOW)

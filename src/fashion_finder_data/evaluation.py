"""Aggregate consented shopper-task labels without storing queries or images."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .public_catalogue import public_offer
from .readiness import _validate_category
from .source_policy import SourcePolicy
from .storage import Database

OfferId = Annotated[int, Field(gt=0)]


class ShopperTask(BaseModel):
    """One manually labeled task; refs point to private consent evidence and input."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=80)
    consent_ref: str = Field(min_length=1, max_length=200)
    input_ref: str = Field(min_length=1, max_length=200)
    category: str
    text_result_ids: list[OfferId] = Field(max_length=50)
    text_useful_ids: list[OfferId]
    text_latency_ms: int = Field(ge=0, le=120_000)
    photo_result_ids: list[OfferId] | None = Field(default=None, max_length=5)
    photo_relevant_ids: list[OfferId] | None = None
    photo_latency_ms: int | None = Field(default=None, ge=0, le=120_000)

    @model_validator(mode="after")
    def check_labels(self) -> ShopperTask:
        for name in (
            "text_result_ids",
            "text_useful_ids",
            "photo_result_ids",
            "photo_relevant_ids",
        ):
            values = getattr(self, name)
            if values is not None and len(values) != len(set(values)):
                raise ValueError(f"{name} contains duplicate offer IDs")
        if not set(self.text_useful_ids).issubset(self.text_result_ids):
            raise ValueError("text useful IDs must occur in text results")
        photo_fields = (self.photo_result_ids, self.photo_relevant_ids, self.photo_latency_ms)
        if any(value is None for value in photo_fields) and not all(
            value is None for value in photo_fields
        ):
            raise ValueError(
                "photo results, relevance labels, and latency must be supplied together"
            )
        if self.photo_result_ids is not None and not set(self.photo_relevant_ids or []).issubset(
            self.photo_result_ids
        ):
            raise ValueError("photo relevant IDs must occur in photo results")
        return self


def evaluate_tasks(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    category: str,
    path: Path,
    *,
    now: datetime | None = None,
) -> dict:
    """Apply the 30-task gate to current offers and self-reported usefulness labels."""
    _validate_category(category)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError("evaluation file must contain a JSON list")
    tasks = [ShopperTask.model_validate(item) for item in raw]
    if len({task.task_id for task in tasks}) != len(tasks):
        raise ValueError("task IDs must be unique")
    if any(task.category != category for task in tasks):
        raise ValueError("all tasks must use the selected category")
    current = now or datetime.now(UTC)
    ids = {
        offer_id
        for task in tasks
        for offer_id in (task.text_result_ids + (task.photo_result_ids or []))
    }
    offers = {offer_id: public_offer(database, offer_id, policies, now=current) for offer_id in ids}
    outcomes = []
    photo_scores = []
    text_top5_scores = []
    text_latencies = []
    photo_latencies = []
    for task in tasks:
        useful = [offers[offer_id] for offer_id in task.text_useful_ids if offers[offer_id]]
        useful = [item for item in useful if item["category"] == category]
        source_count = len({item["source_key"] for item in useful})
        text_success = len(useful) >= 3 and source_count >= 2
        text_latencies.append(task.text_latency_ms)
        outcome = {
            "task_id": task.task_id,
            "current_useful_text_options": len(useful),
            "useful_text_sources": source_count,
            "text_success": text_success,
            "photo_evaluated": False,
        }
        if task.photo_result_ids is not None:
            photo_offers = [offers[offer_id] for offer_id in task.photo_result_ids]
            permitted = all(
                offer is not None
                and offer["category"] == category
                and bool(offer["image_urls"])
                and bool(
                    (policy := policies.get((offer["source_key"], "HU")))
                    and policy.allow_image_display
                    and policy.allow_image_processing
                )
                for offer in photo_offers
            )
            if permitted:
                relevant = sum(
                    offers[offer_id] is not None for offer_id in task.photo_relevant_ids or []
                )
                text_top5 = sum(
                    offer_id in task.text_useful_ids and offers[offer_id] is not None
                    for offer_id in task.text_result_ids[:5]
                )
                photo_scores.append(relevant)
                text_top5_scores.append(text_top5)
                photo_latencies.append(task.photo_latency_ms)
                outcome["photo_evaluated"] = True
                outcome["photo_top5_relevant"] = relevant
                outcome["text_top5_relevant_on_same_task"] = text_top5
            else:
                outcome["photo_exclusion"] = "missing_current_permitted_image_offer"
        outcomes.append(outcome)
    successful = sum(item["text_success"] for item in outcomes)
    return {
        "category": category,
        "measured_at": current.isoformat(),
        "submitted_tasks": len(tasks),
        "required_tasks": 30,
        "tasks_with_three_current_options_from_two_sources": successful,
        "required_successful_tasks": 24,
        "text_task_gate_met": len(tasks) == 30 and successful >= 24,
        "text_latency_median_ms": median(text_latencies) if text_latencies else None,
        "photo_tasks_submitted": sum(task.photo_result_ids is not None for task in tasks),
        "photo_tasks_evaluated": len(photo_scores),
        "photo_top5_relevant_mean": round(mean(photo_scores), 2) if photo_scores else None,
        "text_top5_relevant_mean_on_photo_tasks": round(mean(text_top5_scores), 2)
        if text_top5_scores
        else None,
        "photo_latency_median_ms": median(photo_latencies) if photo_latencies else None,
        "task_outcomes": outcomes,
        "consent_verified_by_tool": False,
        "relevance_labels_are_self_reported": True,
        "policy_evidence_verified_by_tool": False,
    }

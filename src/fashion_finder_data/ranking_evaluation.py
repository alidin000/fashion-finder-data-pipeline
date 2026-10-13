"""Replay private shopper tasks against the current text and image search code."""

from __future__ import annotations

import json
import os
import warnings
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from statistics import mean, median
from time import perf_counter
from typing import Annotated

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .image_index import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, image_index_ready, search_indexed_image
from .public_catalogue import public_offer, search_public_catalogue
from .readiness import _validate_category
from .source_policy import SourcePolicy
from .storage import Database

OfferId = Annotated[int, Field(gt=0)]


class RankingTask(BaseModel):
    """Private input and independent relevance labels for one shopper task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=80)
    consent_ref: str = Field(min_length=1, max_length=200)
    input_ref: str = Field(min_length=1, max_length=200)
    category: str
    query: str = Field(default="", max_length=200)
    colour: str | None = Field(default=None, max_length=80)
    size: str | None = Field(default=None, max_length=40)
    max_price: Decimal | None = None
    photo_path: str | None = Field(default=None, min_length=1, max_length=500)
    relevant_offer_ids: list[OfferId] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def check_task(self) -> RankingTask:
        if len(self.relevant_offer_ids) != len(set(self.relevant_offer_ids)):
            raise ValueError("relevant offer IDs must be unique")
        if self.max_price is not None and (not self.max_price.is_finite() or self.max_price < 0):
            raise ValueError("max price must be finite and nonnegative")
        return self


def _local_photo(root: Path, relative_name: str) -> Image.Image:
    relative = Path(relative_name)
    if relative.is_absolute():
        raise ValueError("task photo path must be relative to the task file")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("task photo path is outside the task directory or missing")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("task photo exceeds 5 MB")
    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("task photo exceeds 5 MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("task photo must be JPEG, PNG, or WebP")
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ValueError("task photo dimensions are too large")
                source.load()
                return source.copy()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("cannot decode task photo") from exc


def evaluate_rankings(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    category: str,
    path: Path,
    *,
    now: datetime | None = None,
) -> dict:
    """Measure actual local rankings against independent, consented labels."""
    _validate_category(category)
    if os.environ.get("FASHION_FINDER_DEMO_MODE") == "1":
        raise ValueError("ranking evaluation cannot run in demo mode")
    raw = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    if not isinstance(raw, list) or not 1 <= len(raw) <= 30:
        raise ValueError("ranking evaluation requires 1–30 tasks")
    try:
        tasks = [RankingTask.model_validate(item) for item in raw]
    except ValidationError as exc:
        # Pydantic's default error includes input values, which can be private queries.
        raise ValueError("invalid ranking task data; check fields and limits") from exc
    if len({task.task_id for task in tasks}) != len(tasks):
        raise ValueError("task IDs must be unique")
    if len({task.input_ref for task in tasks}) != len(tasks):
        raise ValueError("task input references must be unique")
    if any(task.category != category for task in tasks):
        raise ValueError("all tasks must use the selected category")
    if any(task.photo_path for task in tasks) and not image_index_ready(database, policies):
        raise ValueError("no current permitted image index is available")

    current = now or datetime.now(UTC)
    root = path.resolve().parent
    outcomes = []
    text_latencies = []
    photo_latencies = []
    photo_scores = []
    text_top5_scores = []
    for task in tasks:
        relevant = {
            offer_id
            for offer_id in task.relevant_offer_ids
            if (offer := public_offer(database, offer_id, policies, now=current)) is not None
            and offer["category"] == category
            and offer["source_key"] != "demo"
        }
        filters = {
            "category": category,
            "colour": task.colour,
            "size": task.size,
            "max_price": task.max_price,
        }
        started = perf_counter()
        text_items = search_public_catalogue(
            database, policies, query=task.query, limit=20, now=current, **filters
        )["items"]
        text_ms = round((perf_counter() - started) * 1000, 2)
        text_latencies.append(text_ms)
        useful = [item for item in text_items if item["id"] in relevant]
        text_success = len(useful) >= 3 and len({item["source_key"] for item in useful}) >= 2
        text_top5 = sum(item["id"] in relevant for item in text_items[:5])
        outcome = {
            "task_id": task.task_id,
            "current_relevant_offers": len(relevant),
            "excluded_relevant_offers": len(task.relevant_offer_ids) - len(relevant),
            "text_result_ids": [item["id"] for item in text_items],
            "text_top5_relevant": text_top5,
            "text_useful_current_options": len(useful),
            "text_useful_sources": len({item["source_key"] for item in useful}),
            "text_success": text_success,
            "text_latency_ms": text_ms,
            "photo_evaluated": False,
        }
        if task.photo_path:
            started = perf_counter()
            with _local_photo(root, task.photo_path) as photo:
                photo_items = search_indexed_image(database, policies, photo, **filters)["items"]
            photo_ms = round((perf_counter() - started) * 1000, 2)
            photo_top5 = sum(item["id"] in relevant for item in photo_items)
            photo_latencies.append(photo_ms)
            photo_scores.append(photo_top5)
            text_top5_scores.append(text_top5)
            outcome.update(
                {
                    "photo_evaluated": True,
                    "photo_result_ids": [item["id"] for item in photo_items],
                    "photo_top5_relevant": photo_top5,
                    "photo_latency_ms": photo_ms,
                }
            )
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
        "text_latency_median_ms": median(text_latencies),
        "photo_tasks_evaluated": len(photo_scores),
        "photo_top5_relevant_mean": round(mean(photo_scores), 2) if photo_scores else None,
        "text_top5_relevant_mean_on_photo_tasks": round(mean(text_top5_scores), 2)
        if text_top5_scores
        else None,
        "photo_latency_median_ms": median(photo_latencies) if photo_latencies else None,
        "task_outcomes": outcomes,
        "ranking_computed_by_tool": True,
        "consent_verified_by_tool": False,
        "relevance_labels_verified_by_tool": False,
        "policy_evidence_verified_by_tool": False,
    }

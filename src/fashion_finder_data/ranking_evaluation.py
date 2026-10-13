"""Replay private shopper tasks against the current text and image search code."""

from __future__ import annotations

import json
import math
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

from .image_index import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    image_index_ready,
    search_indexed_image,
    search_indexed_text,
    text_index_ready,
)
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


MAX_TASKS = 100  # 30 for the release gate; more give model comparisons more power


def _mean(values: list[int]) -> float | None:
    return round(mean(values), 2) if values else None


def ranking_metrics(result_ids: list[int], relevant: set[int]) -> dict[str, float]:
    """Hit@5, reciprocal rank within the top 10, and binary nDCG@10 for one task."""
    top = result_ids[:10]
    first = next((rank for rank, offer_id in enumerate(top, 1) if offer_id in relevant), None)
    gain = sum(
        1 / math.log2(rank + 1) for rank, offer_id in enumerate(top, 1) if offer_id in relevant
    )
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(relevant), 10) + 1))
    return {
        "hit_at_5": float(first is not None and first <= 5),
        "mrr_at_10": 1 / first if first else 0.0,
        "ndcg_at_10": gain / ideal if ideal else 0.0,
    }


def _summary(per_task: list[dict[str, float]]) -> dict[str, float | int] | None:
    if not per_task:
        return None
    return {
        "tasks": len(per_task),
        **{key: round(mean(item[key] for item in per_task), 3) for key in per_task[0]},
    }


def evaluate_rankings(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    category: str,
    path: Path,
    *,
    now: datetime | None = None,
    model_path: Path | None = None,
    text_model_path: Path | None = None,
    tokenizer_path: Path | None = None,
) -> dict:
    """Measure actual local rankings against independent, consented labels.

    Photo tasks also run without the category filter, with and without the
    category vote, to show whether the vote helps shoppers who pick no category.
    With a text model, each query also runs as a text-to-photo ("look") search.
    """
    _validate_category(category)
    if os.environ.get("FASHION_FINDER_DEMO_MODE") == "1":
        raise ValueError("ranking evaluation cannot run in demo mode")
    raw = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TASKS:
        raise ValueError(f"ranking evaluation requires 1–{MAX_TASKS} tasks")
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
    if any(task.photo_path for task in tasks) and not image_index_ready(
        database, policies, model_path=model_path
    ):
        raise ValueError("no current permitted image index is available")
    text_models = {
        "model_path": model_path,
        "text_model_path": text_model_path,
        "tokenizer_path": tokenizer_path,
    }
    evaluate_look = text_model_path is not None or tokenizer_path is not None
    if evaluate_look and not text_index_ready(database, policies, **text_models):
        raise ValueError("text-to-photo search needs a FashionSigLIP index and text model")

    current = now or datetime.now(UTC)
    root = path.resolve().parent
    outcomes = []
    text_latencies = []
    photo_latencies = []
    photo_scores = []
    photo_vote_scores = []
    photo_plain_scores = []
    text_top5_scores = []
    look_scores = []
    text_top5_on_look = []
    metrics: dict[str, list[dict[str, float]]] = {
        name: []
        for name in (
            "text",
            "photo",
            "photo_any_category",
            "photo_any_category_without_vote",
            "look",
            "text_on_look_tasks",
        )
    }
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
            database,
            policies,
            query=task.query,
            limit=20,
            now=current,
            # The shopper page sorts a typed query by best match.
            sort="relevance" if task.query.strip() else "price-asc",
            **filters,
        )["items"]
        text_ids = [item["id"] for item in text_items]
        metrics["text"].append(ranking_metrics(text_ids, relevant))
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
            any_category = {**filters, "category": None}
            started = perf_counter()
            with _local_photo(root, task.photo_path) as photo:
                photo_items = search_indexed_image(
                    database, policies, photo, model_path=model_path, **filters
                )["items"]
                photo_ms = round((perf_counter() - started) * 1000, 2)
                voted = search_indexed_image(
                    database, policies, photo, model_path=model_path, **any_category
                )["items"]
                plain = search_indexed_image(
                    database,
                    policies,
                    photo,
                    model_path=model_path,
                    consensus=False,
                    **any_category,
                )["items"]
            photo_top5 = sum(item["id"] in relevant for item in photo_items)
            for name, items in (
                ("photo", photo_items),
                ("photo_any_category", voted),
                ("photo_any_category_without_vote", plain),
            ):
                metrics[name].append(ranking_metrics([item["id"] for item in items], relevant))
            photo_latencies.append(photo_ms)
            photo_scores.append(photo_top5)
            photo_vote_scores.append(sum(item["id"] in relevant for item in voted))
            photo_plain_scores.append(sum(item["id"] in relevant for item in plain))
            text_top5_scores.append(text_top5)
            outcome.update(
                {
                    "photo_evaluated": True,
                    "photo_result_ids": [item["id"] for item in photo_items],
                    "photo_top5_relevant": photo_top5,
                    "photo_any_category_top5_relevant": photo_vote_scores[-1],
                    "photo_any_category_without_vote_top5_relevant": photo_plain_scores[-1],
                    "photo_latency_ms": photo_ms,
                }
            )
        if evaluate_look and task.query.strip():
            look_items = search_indexed_text(
                database, policies, task.query, **text_models, **filters
            )["items"]
            look_scores.append(sum(item["id"] in relevant for item in look_items[:5]))
            text_top5_on_look.append(text_top5)
            metrics["look"].append(ranking_metrics([item["id"] for item in look_items], relevant))
            metrics["text_on_look_tasks"].append(metrics["text"][-1])
            outcome.update(
                {
                    "look_result_ids": [item["id"] for item in look_items],
                    "look_top5_relevant": look_scores[-1],
                }
            )
        outcomes.append(outcome)
    successful = sum(item["text_success"] for item in outcomes)
    # The gate is 24 of 30 tasks; larger comparison sets keep the same 80 % share.
    required_successful = math.ceil(0.8 * max(len(tasks), 30))
    return {
        "category": category,
        "measured_at": current.isoformat(),
        "submitted_tasks": len(tasks),
        "required_tasks": 30,
        "tasks_with_three_current_options_from_two_sources": successful,
        "required_successful_tasks": required_successful,
        "text_task_gate_met": len(tasks) >= 30 and successful >= required_successful,
        # Hit@5, MRR@10 and nDCG@10 per search mode; photo search returns 5 results.
        "metrics": {name: _summary(values) for name, values in metrics.items()},
        "text_latency_median_ms": median(text_latencies),
        "photo_tasks_evaluated": len(photo_scores),
        "photo_top5_relevant_mean": round(mean(photo_scores), 2) if photo_scores else None,
        "text_top5_relevant_mean_on_photo_tasks": round(mean(text_top5_scores), 2)
        if text_top5_scores
        else None,
        "photo_latency_median_ms": median(photo_latencies) if photo_latencies else None,
        "photo_any_category_top5_relevant_mean": _mean(photo_vote_scores),
        "photo_any_category_without_vote_top5_relevant_mean": _mean(photo_plain_scores),
        "look_tasks_evaluated": len(look_scores),
        "look_top5_relevant_mean": _mean(look_scores),
        "text_top5_relevant_mean_on_look_tasks": _mean(text_top5_on_look),
        "task_outcomes": outcomes,
        "ranking_computed_by_tool": True,
        "consent_verified_by_tool": False,
        "relevance_labels_verified_by_tool": False,
        "policy_evidence_verified_by_tool": False,
    }

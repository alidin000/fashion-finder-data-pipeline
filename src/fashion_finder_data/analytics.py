"""Low-cardinality anonymous daily counters and bounded retention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from django.db import transaction
from django.db.models import F, Sum
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .catalogue.models import DailyMetric
from .source_policy import SourcePolicy
from .storage import Database


class MetricEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["search", "click", "error"]
    search_type: Literal["text", "photo"]
    filtered: bool = False
    result_count: int | None = Field(default=None, ge=0, le=10000)
    source_key: (
        Literal["zara", "hm", "reserved", "pullandbear", "aboutyou", "answear", "demo"] | None
    ) = None
    error_code: Literal["http_4xx", "http_5xx", "network", "other"] | None = None

    @model_validator(mode="after")
    def check_event(self) -> MetricEvent:
        if self.event_type == "search" and (
            self.result_count is None or self.source_key is not None or self.error_code is not None
        ):
            raise ValueError("search requires result_count and no source or error code")
        if self.event_type == "click" and (
            self.source_key is None or self.result_count is not None or self.error_code is not None
        ):
            raise ValueError("click requires a source and no result count or error code")
        if self.event_type == "error" and (
            self.error_code is None or self.result_count is not None or self.source_key is not None
        ):
            raise ValueError("error requires a code and no result count or source")
        return self


def _bucket(count: int | None) -> str:
    if count is None:
        return ""
    if count == 0:
        return "0"
    if count < 3:
        return "1-2"
    if count < 10:
        return "3-9"
    return "10+"


def record_metric(
    database: Database,
    payload: dict,
    policies: dict[tuple[str, str], SourcePolicy],
    *,
    now: datetime | None = None,
) -> None:
    event = MetricEvent.model_validate(payload)
    if event.source_key:
        policy = policies.get((event.source_key, "HU"))
        if (
            policy is None
            or policy.approval_status != "approved"
            or not policy.allow_public_listing
        ):
            raise ValueError("click source is not currently approved")
    current = now or datetime.now(UTC)
    dimensions = {
        "day": current.astimezone(UTC).date(),
        "event_type": event.event_type,
        "source_key": event.source_key or "",
        "search_type": event.search_type,
        "filtered": event.filtered,
        "result_bucket": _bucket(event.result_count),
        "error_code": event.error_code or "",
    }
    with transaction.atomic(using=database.alias):
        metric, _ = DailyMetric.objects.using(database.alias).get_or_create(
            **dimensions, defaults={"count": 0}
        )
        DailyMetric.objects.using(database.alias).filter(pk=metric.pk).update(count=F("count") + 1)


def metric_report(database: Database, *, days: int = 7, now: datetime | None = None) -> dict:
    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    since = current.date() - timedelta(days=days - 1)
    rows = (
        DailyMetric.objects.using(database.alias)
        .filter(day__gte=since)
        .values(
            "day",
            "event_type",
            "source_key",
            "search_type",
            "filtered",
            "result_bucket",
            "error_code",
        )
        .annotate(total=Sum("count"))
        .order_by(
            "day",
            "event_type",
            "source_key",
            "search_type",
            "filtered",
            "result_bucket",
            "error_code",
        )
    )
    return {
        "from_day": since.isoformat(),
        "through_day": current.date().isoformat(),
        "retention_days": 30,
        "counters": [{**row, "day": row["day"].isoformat()} for row in rows],
    }


def prune_metrics(
    database: Database, *, now: datetime | None = None, retention_days: int = 30
) -> int:
    if not 1 <= retention_days <= 365:
        raise ValueError("retention_days must be between 1 and 365")
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    oldest_kept = today - timedelta(days=retention_days - 1)
    count, _ = DailyMetric.objects.using(database.alias).filter(day__lt=oldest_kept).delete()
    return count

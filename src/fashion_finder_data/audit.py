"""Validate recorded manual audits against the current local catalogue."""

from __future__ import annotations

import csv
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .collectors import SOURCES
from .public_catalogue import _visible
from .readiness import AUDIT_FIELDS, AUDIT_TARGET, _safe_csv_cell, _validate_category
from .source_policy import SourcePolicy
from .storage import Database, offer_rows

VERDICT_FIELDS = (
    "price_verified",
    "category_verified",
    "hungarian_link_verified",
    "availability_and_size_verified",
)
SNAPSHOT_FIELDS = (
    "source_product_id",
    "source_url",
    "category",
    "price_amount",
    "currency",
    "availability_status",
    "size_options",
)


def _parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _read_sample(path: Path, category: str) -> tuple[str, list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        required = set(AUDIT_FIELDS) - {"offer_id"}
        if len(headers) != len(set(headers)) or not required.issubset(headers):
            raise ValueError(f"{path}: missing or duplicate audit CSV columns")
        rows = list(reader)
    if not rows:
        raise ValueError(f"{path}: audit CSV has no offers")
    source = rows[0]["source_key"]
    if source not in SOURCES:
        raise ValueError(f"{path}: unsupported source key: {source}")
    for line_number, row in enumerate(rows, start=2):
        if (
            None in row
            or any(row[field] is None for field in required)
            or row["source_key"] != source
            or row["category"] != category
        ):
            raise ValueError(f"{path}:{line_number}: mixed source/category or malformed row")
        if not row["source_url"]:
            raise ValueError(f"{path}:{line_number}: missing source URL")
        if row["public_eligible"] not in {"yes", "no"}:
            raise ValueError(f"{path}:{line_number}: public_eligible must be yes or no")
        for field in VERDICT_FIELDS:
            if row[field] not in {"", "yes", "no"}:
                raise ValueError(f"{path}:{line_number}: {field} must be yes, no, or blank")
    return source, rows


def audit_report(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    category: str,
    paths: list[Path],
    *,
    now: datetime | None = None,
) -> dict:
    """Summarize self-reported page checks without asserting live verification."""
    _validate_category(category)
    if not paths:
        raise ValueError("at least one audit CSV is required")
    current = now or datetime.now(UTC)
    summaries = []
    seen_sources: set[str] = set()
    for path in paths:
        source, rows = _read_sample(path, category)
        if source in seen_sources:
            raise ValueError(f"more than one audit CSV supplied for {source}")
        seen_sources.add(source)
        policy = policies.get((source, "HU"))
        current_rows = offer_rows(
            database,
            category=category,
            retailer__source_key=source,
            retailer__market_code="HU",
        )
        by_id = {row["id"]: row for row in current_rows}
        by_url = {row["source_url"]: row for row in current_rows}
        seen_urls: set[str] = set()
        issues: Counter[str] = Counter()
        passed = 0
        for line_number, sample in enumerate(rows, start=2):
            url = sample["source_url"]
            if url in seen_urls:
                raise ValueError(f"{path}:{line_number}: duplicate offer URL")
            seen_urls.add(url)
            offer_id = sample.get("offer_id")
            if offer_id:
                try:
                    stored = by_id.get(int(offer_id))
                except ValueError as exc:
                    raise ValueError(f"{path}:{line_number}: invalid offer ID") from exc
            else:
                stored = by_url.get(url)
            reasons: set[str] = set()
            if sample["public_eligible"] != "yes":
                reasons.add("not_eligible_in_sample")
            if stored is None or _visible(stored, policies, current) is None:
                reasons.add("not_currently_eligible")
            if stored is not None and any(
                sample[field] != _safe_csv_cell(str(stored[field] or ""))
                for field in SNAPSHOT_FIELDS
            ):
                reasons.add("catalogue_changed_since_sample")
            verdicts = [sample[field] for field in VERDICT_FIELDS]
            if "no" in verdicts:
                reasons.add("review_rejected")
            if "" in verdicts:
                reasons.add("missing_verdict")
            if not (sample["reviewer"] or "").strip():
                reasons.add("missing_reviewer")
            if not sample["reviewed_at"]:
                reasons.add("missing_review_time")
            else:
                reviewed_at = _parse_time(
                    sample["reviewed_at"], f"{path}:{line_number} reviewed_at"
                )
                sampled_at = _parse_time(
                    sample["last_checked_at"], f"{path}:{line_number} last_checked_at"
                )
                if reviewed_at > current or reviewed_at < sampled_at:
                    reasons.add("review_time_outside_sample")
                if policy and reviewed_at + timedelta(hours=policy.freshness_hours) < current:
                    reasons.add("review_expired")
            if reasons:
                issues.update(reasons)
            else:
                passed += 1
        summaries.append(
            {
                "source_key": source,
                "submitted_offers": len(rows),
                "passed_offers": passed,
                "offers_to_audit_target": max(0, AUDIT_TARGET - passed),
                "recorded_audit_target_met": passed >= AUDIT_TARGET,
                "review_window_hours": policy.freshness_hours if policy else None,
                "issue_counts": dict(sorted(issues.items())),
            }
        )
    return {
        "category": category,
        "measured_at": current.isoformat(),
        "required_sources": 2,
        "required_passed_offers_per_source": AUDIT_TARGET,
        "sources": summaries,
        "two_source_recorded_audit_pass": sum(
            item["recorded_audit_target_met"] for item in summaries
        )
        >= 2,
        "verdicts_are_self_reported": True,
        "live_product_pages_checked_by_tool": False,
        "policy_evidence_verified_by_tool": False,
    }

"""Local release-gate measurements and reproducible manual audit samples."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from .public_catalogue import _safe_image_url, _visible
from .source_policy import SourcePolicy
from .sources import PRODUCTION_SOURCES, SOURCES
from .storage import Database, offer_rows
from .taxonomy import CATEGORIES

OFFER_TARGET = 100
AUDIT_TARGET = 30

AUDIT_FIELDS = (
    "offer_id",
    "source_key",
    "source_product_id",
    "title",
    "category",
    "price_amount",
    "currency",
    "source_url",
    "availability_status",
    "size_options",
    "last_checked_at",
    "public_eligible",
    "reviewer",
    "reviewed_at",
    "price_verified",
    "category_verified",
    "hungarian_link_verified",
    "availability_and_size_verified",
    "reviewer_notes",
)


def _validate_category(category: str) -> None:
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of: {', '.join(sorted(CATEGORIES))}")


def _image_candidate(row: dict, policy: SourcePolicy) -> bool:
    if not policy.allow_image_display or not policy.allow_image_processing:
        return False
    try:
        images = json.loads(row["image_urls"])
    except (TypeError, ValueError):
        return False
    if not isinstance(images, list):
        return False
    return any(isinstance(url, str) and _safe_image_url(url) for url in images)


def readiness_report(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    category: str,
    *,
    now: datetime | None = None,
) -> dict:
    """Count offers under the same visibility rule as the shopper API."""
    _validate_category(category)
    current = now or datetime.now(UTC)
    rows = offer_rows(database, category=category, retailer__market_code="HU")
    sources = []
    for source_key in PRODUCTION_SOURCES:
        policy = policies.get((source_key, "HU"))
        own_rows = [row for row in rows if row["source_key"] == source_key]
        visible = [(row, _visible(row, policies, current)) for row in own_rows]
        eligible = [(row, granted) for row, granted in visible if granted is not None]
        image_count = sum(_image_candidate(row, granted) for row, granted in eligible)
        sources.append(
            {
                "source_key": source_key,
                "declared_policy_status": policy.approval_status if policy else "absent",
                "public_listing_allowed": bool(policy and policy.allow_public_listing),
                "image_display_allowed": bool(policy and policy.allow_image_display),
                "image_processing_allowed": bool(policy and policy.allow_image_processing),
                "stored_offers": len(own_rows),
                "public_eligible_offers": len(eligible),
                "offers_to_text_target": max(0, OFFER_TARGET - len(eligible)),
                "image_candidates": image_count,
                "images_to_photo_target": max(0, OFFER_TARGET - image_count),
                "text_coverage_target_met": len(eligible) >= OFFER_TARGET,
                "photo_coverage_target_met": image_count >= OFFER_TARGET,
            }
        )
    return {
        "category": category,
        "measured_at": current.isoformat(),
        "required_sources": 2,
        "required_offers_per_source": OFFER_TARGET,
        "manual_audit_per_source": AUDIT_TARGET,
        "sources": sources,
        "text_coverage_pass": sum(item["text_coverage_target_met"] for item in sources) >= 2,
        "photo_coverage_pass": sum(item["photo_coverage_target_met"] for item in sources) >= 2,
        "policy_evidence_verified_by_tool": False,
        "remaining_human_checks": [
            "Confirm source permissions and image rights against the cited agreements",
            "Manually audit 30 offers per launch source",
            "Evaluate 30 consented shopping tasks and photo top-5 relevance",
            "Run a deployed end-to-end live smoke test",
        ],
    }


def _safe_csv_cell(value: object) -> object:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_audit_sample(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    category: str,
    source_key: str,
    output: Path,
    *,
    count: int = AUDIT_TARGET,
    all_stored: bool = False,
    now: datetime | None = None,
) -> int:
    """Create a stable CSV sample; default to offers the API could publish."""
    _validate_category(category)
    if source_key not in SOURCES:
        raise ValueError(f"unknown source: {source_key}")
    if source_key == "demo":
        raise ValueError("demo offers cannot be used for a release audit")
    if not 1 <= count <= 100:
        raise ValueError("count must be between 1 and 100")
    current = now or datetime.now(UTC)
    rows = offer_rows(
        database,
        category=category,
        retailer__source_key=source_key,
        retailer__market_code="HU",
    )
    candidates = [(row, _visible(row, policies, current) is not None) for row in rows]
    if not all_stored:
        candidates = [(row, visible) for row, visible in candidates if visible]
    candidates.sort(
        key=lambda item: hashlib.sha256(
            f"{source_key}:{category}:{item[0]['id']}".encode()
        ).digest()
    )
    sample = candidates[:count]
    if not sample:
        raise ValueError("no offers in the selected audit scope; no CSV was created")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=AUDIT_FIELDS)
        writer.writeheader()
        for row, visible in sample:
            values = {
                "offer_id": row["id"],
                "source_key": source_key,
                "source_product_id": row["source_product_id"],
                "title": row["title"],
                "category": row["category"],
                "price_amount": row["price_amount"],
                "currency": row["currency"],
                "source_url": row["source_url"],
                "availability_status": row["availability_status"],
                "size_options": row["size_options"],
                "last_checked_at": row["source_fetched_at"],
                "public_eligible": "yes" if visible else "no",
                "reviewer": "",
                "reviewed_at": "",
                "price_verified": "",
                "category_verified": "",
                "hungarian_link_verified": "",
                "availability_and_size_verified": "",
                "reviewer_notes": "",
            }
            writer.writerow({key: _safe_csv_cell(value) for key, value in values.items()})
    return len(sample)

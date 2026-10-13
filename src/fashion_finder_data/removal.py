"""Source-specific, read-only listing recheck policy.

Category samples cannot prove that a listing disappeared. This module only
prioritizes direct checks; it never changes offer or stock state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .schema import utc_now
from .sources import SOURCES, allowed_url
from .storage import Database, offer_rows

LPP_POLICY = {
    "web_evidence": (
        "explicit Product offer availability on an accessible product page, "
        "or per-size stock flags in the embedded category listing"
    ),
    "missing_from_sample": "recheck",
    "http_403_429_5xx": "recheck",
    "http_404_410": "manual_review",
}

SOURCE_POLICY = {
    "reserved": LPP_POLICY,
    "sinsay": LPP_POLICY,
    "mohito": LPP_POLICY,
    "house": LPP_POLICY,
    "cropp": LPP_POLICY,
    "hm": {
        "web_evidence": "none verified; category cards do not establish stock",
        "missing_from_sample": "recheck",
        "http_403_429_5xx": "recheck",
        "http_404_410": "manual_review",
    },
    "zara": {
        "web_evidence": "none verified",
        "missing_from_sample": "recheck",
        "http_403_429_5xx": "recheck",
        "http_404_410": "manual_review",
    },
    "pullandbear": {
        "web_evidence": "none verified",
        "missing_from_sample": "recheck",
        "http_403_429_5xx": "recheck",
        "http_404_410": "manual_review",
    },
    "aboutyou": {
        "web_evidence": "none verified; feed observations require partner access",
        "missing_from_sample": "recheck",
        "http_403_429_5xx": "recheck",
        "http_404_410": "manual_review",
    },
    "answear": {
        "web_evidence": "none verified; feed observations require partner access",
        "missing_from_sample": "recheck",
        "http_403_429_5xx": "recheck",
        "http_404_410": "manual_review",
    },
    "demo": {
        "web_evidence": "synthetic local fixture; no retailer page exists",
        "missing_from_sample": "manual_review",
        "http_403_429_5xx": "manual_review",
        "http_404_410": "manual_review",
    },
}


def recheck_report(
    engine: Database,
    *,
    stale_after_days: int = 7,
    source_key: str | None = None,
    limit: int = 100,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """List stale or malformed offers; leave persistence untouched."""
    if not 1 <= stale_after_days <= 3650:
        raise ValueError("stale-after-days must be between 1 and 3650")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if source_key is not None and source_key not in SOURCES:
        raise ValueError("unknown source")
    now = as_of or utc_now()
    if now.tzinfo is None:
        raise ValueError("as-of timestamp must have a timezone")
    cutoff = now.astimezone(UTC) - timedelta(days=stale_after_days)
    rows = offer_rows(engine)
    if source_key:
        rows = [row for row in rows if row["source_key"] == source_key]
    candidates = []
    for row in rows:
        timestamp_problem = None
        try:
            seen = datetime.fromisoformat(row["last_seen_at"])
            if seen.tzinfo is None:
                raise ValueError("timestamp has no timezone")
            seen = seen.astimezone(UTC)
            stale = seen < cutoff
            if seen > now:
                timestamp_problem = "future_timestamp"
        except (TypeError, ValueError):
            stale = True
            timestamp_problem = "invalid_timestamp"
        source = SOURCES.get(row["source_key"])
        valid_url = source is not None and allowed_url(source, row["canonical_url"])
        if not stale and valid_url and timestamp_problem is None:
            continue
        reason = "invalid_url" if not valid_url else timestamp_problem or "stale"
        candidates.append(
            {
                "source_key": row["source_key"],
                "source_product_id": row["source_product_id"],
                "product_url": row["canonical_url"] if valid_url else None,
                "last_seen_at": row["last_seen_at"],
                "reason": reason,
                "action": "manual_review"
                if not valid_url or timestamp_problem
                else "direct_recheck"
                if source and source.crawl_enabled
                else "manual_review",
            }
        )
    candidates.sort(key=lambda item: (item["source_key"], item["last_seen_at"] or ""))
    return {
        "generated_at": now.isoformat(),
        "stale_after_days": stale_after_days,
        "candidate_count": len(candidates),
        "candidates": candidates[:limit],
        "policy": {source_key: SOURCE_POLICY[source_key]} if source_key else SOURCE_POLICY,
    }

"""Explicit publication policy for each retailer and market.

Collection can be useful for local evaluation without granting publication rights.
An absent policy therefore denies public display and image processing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SourcePolicy(BaseModel):
    source_key: Literal["zara", "hm", "reserved", "pullandbear", "aboutyou", "answear", "demo"]
    market_code: Literal["HU"] = "HU"
    approval_status: Literal["pending", "approved", "revoked"] = "pending"
    evidence_ref: str | None = None
    access_method: str | None = None
    allow_public_listing: bool = False
    allow_image_display: bool = False
    allow_image_processing: bool = False
    attribution: str | None = None
    freshness_hours: int = Field(default=72, ge=1, le=24 * 365)
    policy_version: str = "1"

    @model_validator(mode="after")
    def check_approval(self) -> SourcePolicy:
        if self.approval_status != "approved" and (
            self.allow_public_listing or self.allow_image_display or self.allow_image_processing
        ):
            raise ValueError("unapproved source cannot grant publication or image permissions")
        if self.approval_status == "approved" and not self.evidence_ref:
            raise ValueError("approved source requires an evidence reference")
        if (
            self.allow_image_display or self.allow_image_processing
        ) and not self.allow_public_listing:
            raise ValueError("image permissions require public listing permission")
        return self


def load_policies(path: Path | None) -> dict[tuple[str, str], SourcePolicy]:
    """Return no permissions when no policy file is configured."""
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError("source policy file must contain a list")
    result: dict[tuple[str, str], SourcePolicy] = {}
    for entry in raw:
        policy = SourcePolicy.model_validate(entry)
        key = (policy.source_key, policy.market_code)
        if key in result:
            raise ValueError(f"duplicate source policy: {key}")
        result[key] = policy
    return result

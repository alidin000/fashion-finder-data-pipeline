"""Seed an isolated, explicitly synthetic catalogue for local UI testing."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .demo_images import DEMO_IMAGE_FILES, build_demo_image_index
from .pipeline import ingest_records
from .schema import RawProductRecord, utc_now
from .storage import open_database

DEMO_HOST = "https://fashion-finder-demo.invalid/hu"
DEMO_PRODUCTS = (
    ("001", "Navy cropped jacket", "jackets", "Navy", "S,M", "12990"),
    ("002", "Black quilted jacket", "jackets", "Black", "M,L", "14990"),
    ("003", "Brown flight jacket", "jackets", "Brown", "S,M,L", "18990"),
    ("004", "Cream denim jacket", "jackets", "Cream", "XS,S", "9990"),
    ("005", "Brown wool coat", "coats", "Brown", "M,L", "29990"),
    ("006", "Black long coat", "coats", "Black", "S,M", "34990"),
    ("007", "Blue wrap dress", "dresses", "Blue", "S,M", "11990"),
    ("008", "Red dress", "dresses", "Red", "M,L", "13990"),
    ("009", "Grey knit sweater", "sweaters", "Grey", "S,M,L", "7990"),
    ("010", "White cotton shirt", "shirts", "White", "M,L", "6990"),
    ("011", "Blue loose jeans", "jeans", "Blue", "S,M,L", "10990"),
    ("012", "Tan canvas shoes", "shoes", "Tan", "38,39,40", "15990"),
)
DEMO_POLICY = [
    {
        "source_key": "demo",
        "market_code": "HU",
        "approval_status": "approved",
        "evidence_ref": "docs/demo-image-credits.md and synthetic local demo fixtures",
        "access_method": "synthetic-demo",
        "allow_public_listing": True,
        "allow_image_display": True,
        "allow_image_processing": True,
        "attribution": "Synthetic demo item; no retailer link",
        "freshness_hours": 24 * 365,
        "policy_version": "demo-2",
    }
]
MARKER = "Fashion Finder synthetic demo catalogue\n"
LEGACY_DEMO_POLICY = [
    {
        **DEMO_POLICY[0],
        "evidence_ref": "repository-owned synthetic local demo fixtures",
        "allow_image_display": False,
        "allow_image_processing": False,
        "policy_version": "demo-1",
    }
]


def seed_demo(data_dir: Path) -> dict:
    """Create or refresh a demo database without touching an existing real catalogue."""
    if os.environ.get("FASHION_FINDER_DEMO_MODE") != "1":
        raise ValueError("set FASHION_FINDER_DEMO_MODE=1 to seed synthetic data")
    data_dir = data_dir.resolve()
    marker = data_dir / ".demo-catalogue"
    database_path = data_dir / "fashion_finder.sqlite"
    policy_path = data_dir / "source-policies.json"
    policy_body = json.dumps(DEMO_POLICY, indent=2) + "\n"
    if database_path.exists() and not marker.exists():
        raise ValueError("refusing to seed an existing catalogue without the demo marker")
    if marker.exists() and marker.read_text(encoding="utf-8") != MARKER:
        raise ValueError("demo marker has unexpected content")
    if policy_path.exists() and policy_path.read_text(encoding="utf-8") != policy_body:
        legacy_body = json.dumps(LEGACY_DEMO_POLICY, indent=2) + "\n"
        if not marker.exists() or policy_path.read_text(encoding="utf-8") != legacy_body:
            raise ValueError("refusing to replace an existing source policy")
        policy_path.write_text(policy_body, encoding="utf-8")
    data_dir.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text(MARKER, encoding="utf-8")
    if not policy_path.exists():
        policy_path.write_text(policy_body, encoding="utf-8")
    database = open_database(database_path)
    observed = utc_now()
    records = [
        RawProductRecord(
            source_key="demo",
            market_code="HU",
            source_url=f"{DEMO_HOST}/item-{sku}",
            fetched_at=observed,
            adapter_version="synthetic-demo-v1",
            product={
                "url": f"{DEMO_HOST}/item-{sku}",
                "sku": f"demo-{sku}",
                "name": title,
                "category": category,
                "price": price,
                "currency": "HUF",
                "colours": [colour],
                "sizes": sizes.split(","),
                "availability": "unknown",
                "description": "Synthetic item for local search and filter testing",
                "images": [f"{DEMO_HOST}/images/demo-{sku}.jpg"]
                if f"demo-{sku}" in DEMO_IMAGE_FILES
                else [],
            },
        )
        for sku, title, category, colour, sizes, price in DEMO_PRODUCTS
    ]
    report = ingest_records(database, data_dir, "demo", records)
    build_demo_image_index(data_dir)
    return {
        "status": report["status"],
        "data_dir": str(data_dir),
        "records_valid": report["records_valid"],
        "demo_mode_required": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = seed_demo(args.data_dir)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

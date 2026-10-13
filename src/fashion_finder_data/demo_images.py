"""Small, deterministic visual retrieval for CC0 images in the local demo."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from importlib.resources import files
from io import BytesIO
from pathlib import Path

from PIL import Image

from .public_catalogue import search_public_catalogue
from .source_policy import SourcePolicy
from .storage import Database
from .visual_features import FEATURE_VERSION, feature_distance, image_features

DEMO_IMAGE_FILES = {
    "demo-003": "flight-jacket.jpg",
    "demo-008": "red-dress.jpg",
    "demo-009": "sweater.jpg",
    "demo-011": "blue-jeans.jpg",
    "demo-012": "canvas-shoes.jpg",
}
INDEX_VERSION = 2
INDEX_FILE = "demo-image-index.json"


def demo_image_bytes(sku: str) -> bytes | None:
    """Return only one of the packaged local demo images."""
    name = DEMO_IMAGE_FILES.get(sku)
    return files("fashion_finder_data").joinpath("demo_assets", name).read_bytes() if name else None


def build_demo_image_index(data_dir: Path) -> None:
    entries = []
    for sku in DEMO_IMAGE_FILES:
        data = demo_image_bytes(sku)
        assert data is not None
        with Image.open(BytesIO(data)) as image:
            entries.append(
                {
                    "sku": sku,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "features": image_features(image),
                }
            )
    target = data_dir / INDEX_FILE
    temporary = data_dir / f".{INDEX_FILE}.tmp"
    temporary.write_text(
        json.dumps(
            {"version": INDEX_VERSION, "feature_version": FEATURE_VERSION, "entries": entries}
        ),
        encoding="utf-8",
    )
    temporary.replace(target)


def demo_index_ready(database: Database, policies: dict[tuple[str, str], SourcePolicy]) -> bool:
    policy = policies.get(("demo", "HU"))
    permitted = bool(
        policy
        and policy.approval_status == "approved"
        and policy.allow_public_listing
        and policy.allow_image_display
        and policy.allow_image_processing
        and (database.path.parent / ".demo-catalogue").exists()
        and (database.path.parent / INDEX_FILE).is_file()
    )
    if not permitted:
        return False
    try:
        index = json.loads((database.path.parent / INDEX_FILE).read_text(encoding="utf-8"))
        return (
            isinstance(index, dict)
            and index.get("version") == INDEX_VERSION
            and index.get("feature_version") == FEATURE_VERSION
            and isinstance(index.get("entries"), list)
        )
    except (OSError, ValueError):
        return False


def search_demo_image(
    database: Database,
    policies: dict[tuple[str, str], SourcePolicy],
    image: Image.Image,
    *,
    category: str | None = None,
    colour: str | None = None,
    size: str | None = None,
    max_price: Decimal | None = None,
) -> dict:
    index = json.loads((database.path.parent / INDEX_FILE).read_text(encoding="utf-8"))
    if index.get("version") != INDEX_VERSION or index.get("feature_version") != FEATURE_VERSION:
        raise ValueError("demo image index version is unsupported")
    allowed = search_public_catalogue(
        database,
        policies,
        category=category,
        colour=colour,
        size=size,
        max_price=max_price,
        source="demo",
        limit=50,
    )["items"]
    by_sku = {item["source_product_id"]: item for item in allowed if item["image_urls"]}
    query = image_features(image)
    ranked = []
    for entry in index["entries"]:
        offer = by_sku.get(entry["sku"])
        features = entry["features"]
        if offer is None or len(features) != len(query):
            continue
        ranked.append((feature_distance(query, features), offer["id"], offer))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return {"items": [offer for _, _, offer in ranked[:5]], "next_cursor": None}

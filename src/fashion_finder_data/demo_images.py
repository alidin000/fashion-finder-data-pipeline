"""Small, deterministic visual retrieval for CC0 images in the local demo."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from importlib.resources import files
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from .public_catalogue import search_public_catalogue
from .source_policy import SourcePolicy
from .storage import Database

DEMO_IMAGE_FILES = {
    "demo-003": "flight-jacket.jpg",
    "demo-008": "red-dress.jpg",
    "demo-009": "sweater.jpg",
    "demo-011": "blue-jeans.jpg",
    "demo-012": "canvas-shoes.jpg",
}
INDEX_VERSION = 1
INDEX_FILE = "demo-image-index.json"


def demo_image_bytes(sku: str) -> bytes | None:
    """Return only one of the packaged local demo images."""
    name = DEMO_IMAGE_FILES.get(sku)
    return files("fashion_finder_data").joinpath("demo_assets", name).read_bytes() if name else None


def image_features(image: Image.Image) -> list[float]:
    """Encode coarse colour and layout; this is not a semantic clothing model."""
    rgb = ImageOps.exif_transpose(image).convert("RGB")
    small = rgb.resize((16, 16), Image.Resampling.BILINEAR)
    pixels = [channel / 255 for channel in small.tobytes()]
    histogram = []
    for channel in small.split():
        bins = channel.histogram()
        histogram.extend(sum(bins[index : index + 32]) / 256 for index in range(0, 256, 32))
    return pixels + histogram


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
        json.dumps({"version": INDEX_VERSION, "entries": entries}), encoding="utf-8"
    )
    temporary.replace(target)


def demo_index_ready(database: Database, policies: dict[tuple[str, str], SourcePolicy]) -> bool:
    policy = policies.get(("demo", "HU"))
    return bool(
        policy
        and policy.approval_status == "approved"
        and policy.allow_public_listing
        and policy.allow_image_display
        and policy.allow_image_processing
        and (database.path.parent / ".demo-catalogue").exists()
        and (database.path.parent / INDEX_FILE).is_file()
    )


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
    if index.get("version") != INDEX_VERSION:
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
        pixel_distance = sum(abs(a - b) for a, b in zip(query[:768], features[:768])) / 768
        colour_distance = sum(abs(a - b) for a, b in zip(query[768:], features[768:])) / 24
        ranked.append((0.7 * pixel_distance + 0.3 * colour_distance, offer["id"], offer))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return {"items": [offer for _, _, offer in ranked[:5]], "next_cursor": None}

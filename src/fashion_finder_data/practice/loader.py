"""Load the practice catalogue into an isolated data directory through the real storage path."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..image_index import INDEX_FILE, build_image_index
from ..schema import RetailerOffer
from ..source_policy import SourcePolicy, load_policies
from ..storage import offer_rows, open_database, upsert_offer
from ..visual_encoder import get_encoder
from . import CATALOGUE_FILE, HOST, SOURCE_KEY
from .normalise import PracticeProduct

MARKER_FILE = ".practice-catalogue"
MARKER = "Fashion Finder practice catalogue: public datasets, synthetic prices, not a shop\n"
POLICY_FILE = "source-policies.json"
IMAGE_DIR = "practice-images"
MANIFEST_FILE = "practice-image-manifest.json"
PARTS_DIR = ".practice-index-parts"
CHUNK_PHOTOS = 2000  # photos encoded per run, so one run never holds the machine for long
ATTRIBUTION = (
    "Practice data, not for sale: ABO © Amazon.com (CC BY 4.0); "
    "GLAMI-1M © Inspigroup s.r.o. (Apache-2.0)"
)
POLICY = SourcePolicy(
    source_key=SOURCE_KEY,
    approval_status="approved",
    evidence_ref=(
        "Practice data only, never a retailer: Amazon Berkeley Objects (CC BY 4.0, credit "
        "Amazon.com) and GLAMI-1M (Apache-2.0); licences and credits in the practice folder"
    ),
    access_method="practice-dataset",
    allow_public_listing=True,
    allow_image_display=True,
    allow_image_processing=True,
    attribution=ATTRIBUTION,
    freshness_hours=24 * 365,
    policy_version="practice-1",
)


def policy_body() -> str:
    return json.dumps([POLICY.model_dump()], indent=2) + "\n"


def read_catalogue(practice_dir: Path) -> list[PracticeProduct]:
    path = practice_dir / CATALOGUE_FILE
    products = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, start=1):
            try:
                products.append(PracticeProduct.model_validate_json(line))
            except ValidationError as exc:
                raise ValueError(f"{path.name} line {number} is not a practice product") from exc
    if len({product.practice_id for product in products}) != len(products):
        raise ValueError("practice catalogue repeats a product")
    return products


def balanced(products: list[PracticeProduct], limit: int | None) -> list[PracticeProduct]:
    """At most ``limit`` products, taking each category in turn in catalogue order."""
    if limit is None or limit >= len(products):
        return products
    if limit < 1:
        raise ValueError("limit must be positive")
    queues: dict[str, list[PracticeProduct]] = defaultdict(list)
    for product in products:
        queues[product.category].append(product)
    chosen: set[str] = set()
    while len(chosen) < limit:
        for queue in queues.values():
            if queue and len(chosen) < limit:
                chosen.add(queue.pop(0).practice_id)
    return [product for product in products if product.practice_id in chosen]


def product_url(product: PracticeProduct) -> str:
    return f"https://{HOST}/hu/{product.dataset}/{product.practice_id}"


def _guard(data_dir: Path) -> None:
    """Refuse any data directory that holds, or is set up for, anything but practice rows."""
    if (data_dir / ".demo-catalogue").exists():
        raise ValueError("refusing to load practice data into the demo catalogue")
    marker = data_dir / MARKER_FILE
    if marker.exists() and marker.read_text(encoding="utf-8") != MARKER:
        raise ValueError("practice marker has unexpected content")
    policy_path = data_dir / POLICY_FILE
    if policy_path.exists():
        try:
            keys = {key for key, _market in load_policies(policy_path)}
        except (TypeError, ValueError) as exc:
            raise ValueError("refusing to replace an unreadable source policy file") from exc
        if keys - {SOURCE_KEY}:
            raise ValueError(
                "refusing to load practice data: the source policy names real sources "
                f"({', '.join(sorted(keys - {SOURCE_KEY}))})"
            )
    database_path = data_dir / "fashion_finder.sqlite"
    if database_path.exists():
        database = open_database(database_path)
        others = sorted({row["source_key"] for row in offer_rows(database)} - {SOURCE_KEY})
        if others:
            raise ValueError(
                "refusing to load practice data into a catalogue with real sources "
                f"({', '.join(others)})"
            )
        if not marker.exists() and offer_rows(database):
            raise ValueError("refusing to load practice data into an unmarked catalogue")


def _place(source: Path, target: Path) -> None:
    """Hard-link a photo into the data directory, or copy it across file systems."""
    if target.is_file() and target.stat().st_size == source.stat().st_size:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def load_practice_catalogue(
    data_dir: Path,
    practice_dir: Path,
    *,
    limit: int | None = None,
    exclude_products: frozenset[str] = frozenset(),
    exclude_photos: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create or refresh practice offers, their policy, photos and image manifest.

    ``exclude_products`` leaves products out entirely and ``exclude_photos`` (catalogue
    photo paths) keeps photos out of the manifest; benchmarks use them to hold out
    queries. Rerunning is safe: offers are upserted by product ID, and practice offers
    no longer selected are withdrawn rather than left searchable.
    """
    data_dir = data_dir.resolve()
    practice_dir = practice_dir.resolve()
    if data_dir == practice_dir or data_dir.is_relative_to(practice_dir):
        raise ValueError("use a data directory outside the shared practice folder")
    products = [
        product
        for product in balanced(read_catalogue(practice_dir), limit)
        if product.practice_id not in exclude_products
    ]
    for product in products:
        for image in product.images:
            if not (practice_dir / image.path).is_file():
                raise ValueError(f"practice photo is missing: {image.path}")
    data_dir.mkdir(parents=True, exist_ok=True)
    _guard(data_dir)
    marker = data_dir / MARKER_FILE
    if not marker.exists():
        marker.write_text(MARKER, encoding="utf-8")
    policy_path = data_dir / POLICY_FILE
    if not policy_path.exists() or policy_path.read_text(encoding="utf-8") != policy_body():
        policy_path.write_text(policy_body(), encoding="utf-8")
    database = open_database(data_dir / "fashion_finder.sqlite")
    observed = now or datetime.now(UTC)
    changed = 0
    for product in products:
        url = product_url(product)
        changed += upsert_offer(
            database,
            RetailerOffer(
                source_key=SOURCE_KEY,
                source_url=url,
                canonical_url=url,
                source_product_id=product.practice_id,
                title=product.title,
                brand=product.brand,
                category=product.category,
                source_category=product.source_category,
                description=product.description,
                price_amount=product.price_huf,
                currency="HUF",
                image_urls=[image.url for image in product.images],
                colour_names=product.colour_names,
                size_options=product.sizes,
                availability_status="unknown",
                source_fetched_at=observed,
            ),
        )
    from django.apps import apps

    Retailer = apps.get_model("catalogue", "Retailer")
    Offer = apps.get_model("catalogue", "RetailerOffer")
    Retailer.objects.using(database.alias).filter(source_key=SOURCE_KEY).update(name=ATTRIBUTION)
    selected = [product.practice_id for product in products]
    withdrawn = (
        Offer.objects.using(database.alias)
        .filter(retailer__source_key=SOURCE_KEY, status="active")
        .exclude(source_product_id__in=selected)
        .update(status="withdrawn")
    )
    ids = {
        row["source_product_id"]: row["id"]
        for row in offer_rows(database, retailer__source_key=SOURCE_KEY, status="active")
    }
    manifest = []
    for product in products:
        for image in product.images:
            if image.path in exclude_photos:
                continue
            relative = f"{IMAGE_DIR}/{image.path.removeprefix('images/')}"
            _place(practice_dir / image.path, data_dir / relative)
            manifest.append(
                {
                    "offer_id": ids[product.practice_id],
                    "image_url": image.url,
                    "image_path": relative,
                }
            )
    manifest_path = data_dir / MANIFEST_FILE
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    categories = Counter(product.category for product in products)
    return {
        "status": "loaded",
        "data_dir": str(data_dir),
        "products": len(products),
        "products_by_category": dict(sorted(categories.items())),
        "changed_offers": changed,
        "withdrawn_offers": withdrawn,
        "manifest_photos": len(manifest),
        "manifest_path": str(manifest_path),
        "policy_path": str(policy_path),
        "practice_data_not_a_shop": True,
    }


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_practice_index(
    data_dir: Path,
    *,
    model_path: Path | None = None,
    chunk_photos: int = CHUNK_PHOTOS,
) -> dict[str, Any]:
    """Build the image index with the real ``build_image_index``, one chunk per run.

    Each run encodes at most ``chunk_photos`` photos, saves that part, and stops; when
    every part exists they are joined into the index the API reads. Parts are keyed by
    the encoder version and the chunk's manifest entries, so a rerun after a finished
    build, or after an interrupted one, encodes only what is missing.
    """
    data_dir = data_dir.resolve()
    if not (data_dir / MARKER_FILE).exists():
        raise ValueError("load the practice catalogue before building its index")
    if not 1 <= chunk_photos <= CHUNK_PHOTOS:
        raise ValueError(f"chunk size must be between 1 and {CHUNK_PHOTOS} photos")
    database = open_database(data_dir / "fashion_finder.sqlite")
    policies = load_policies(data_dir / POLICY_FILE)
    entries = json.loads((data_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    version = get_encoder(model_path).version
    chunks = [
        entries[start : start + chunk_photos] for start in range(0, len(entries), chunk_photos)
    ]
    keys = [
        hashlib.sha256(json.dumps([version, chunk], sort_keys=True).encode()).hexdigest()[:20]
        for chunk in chunks
    ]
    parts = data_dir / PARTS_DIR
    parts.mkdir(exist_ok=True)
    index_path = data_dir / INDEX_FILE
    record = parts / "merged.json"
    if index_path.exists() and record.exists():
        merged = json.loads(record.read_text(encoding="utf-8"))
        if merged.get("parts") == keys and merged.get("sha256") == _sha256(index_path):
            return {"index_status": "complete", "parts": len(keys), "encoded_photos": 0}
    for stale in parts.glob("part-*.json"):
        if stale.stem.removeprefix("part-") not in keys:
            stale.unlink()
    encoded = 0
    for chunk, key in zip(chunks, keys, strict=True):
        part = parts / f"part-{key}.json"
        if part.exists():
            continue
        if encoded:
            index_path.unlink(missing_ok=True)  # never serve a partial practice index
            done = sum((parts / f"part-{other}.json").exists() for other in keys)
            return {
                "index_status": "partial",
                "parts_done": done,
                "parts": len(keys),
                "encoded_photos": encoded,
                "next_step": "run the same command again to encode the next part",
            }
        chunk_manifest = data_dir / ".practice-chunk-manifest.json"
        chunk_manifest.write_text(json.dumps(chunk), encoding="utf-8")
        try:
            report = build_image_index(database, policies, chunk_manifest, model_path=model_path)
        finally:
            chunk_manifest.unlink(missing_ok=True)
        if report["status"] != "indexed":
            raise ValueError(f"practice photos were skipped: {report['skipped_images']}")
        index_path.replace(part)
        encoded += report["indexed_images"]
    joined: dict[str, Any] | None = None
    for key in keys:
        piece = json.loads((parts / f"part-{key}.json").read_text(encoding="utf-8"))
        if joined is None:
            joined = piece
        elif {k: v for k, v in piece.items() if k != "entries"} != {
            k: v for k, v in joined.items() if k != "entries"
        }:
            raise ValueError("index parts were built with different settings; delete them")
        else:
            joined["entries"].extend(piece["entries"])
    temporary = index_path.with_name(f".{INDEX_FILE}.tmp")
    temporary.write_text(json.dumps(joined, ensure_ascii=False), encoding="utf-8")
    temporary.replace(index_path)
    record.write_text(
        json.dumps({"parts": keys, "sha256": _sha256(index_path)}) + "\n", encoding="utf-8"
    )
    return {
        "index_status": "complete",
        "parts": len(keys),
        "encoded_photos": encoded,
        "indexed_photos": len(joined["entries"]) if joined else 0,
        "feature_version": version,
        "index_path": str(index_path),
    }


def main(argv: list[str] | None = None) -> int:
    """``python -m fashion_finder_data.practice.loader``: the CLI command without cli.py."""
    import argparse

    parser = argparse.ArgumentParser(description=load_practice_catalogue.__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("practice_dir", type=Path)
    parser.add_argument("--limit", type=int, help="Products, balanced across categories")
    parser.add_argument("--build-index", action="store_true")
    parser.add_argument("--model-path", type=Path)
    args = parser.parse_args(argv)
    try:
        result = load_practice_catalogue(args.data_dir, args.practice_dir, limit=args.limit)
        if args.build_index:
            result["index"] = build_practice_index(args.data_dir, model_path=args.model_path)
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

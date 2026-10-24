"""Download licence-safe public dataset subsets and write a practice catalogue folder.

    python -m fashion_finder_data.practice.build --out <practice-dir>

Sources (see docs/data-research/public-datasets.md):
- ABO, Amazon Berkeley Objects (CC BY 4.0, credit Amazon.com): shoes, bags and
  accessories with several catalogue photos each. Listings come from the 83 MB
  metadata archive; photos are fetched one by one from the public S3 bucket.
- GLAMI-1M (Apache-2.0): Hungarian (`geo == "hu"`), human-labelled rows for every
  garment category. Only the CSVs and the chosen photos are read from the 11 GB
  archive, using HTTP range requests.

Everything is written under ``--out``, which must be outside the repository.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import shutil
import tarfile
import urllib.request
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image

from ..sources import USER_AGENT
from ..taxonomy import CATEGORIES
from . import CATALOGUE_FILE, LOCAL_IMAGE_PREFIX
from .normalise import (
    ABO_CATEGORIES,
    ABO_CREDIT,
    GLAMI_CREDIT,
    PracticeImage,
    PracticeProduct,
    abo_title,
    colour_in,
    glami_category,
    glami_gender,
    model_key,
    stable_fraction,
    synthetic_price,
    synthetic_sizes,
    title_gender,
)

ABO_BASE = "https://amazon-berkeley-objects.s3.amazonaws.com"
ABO_LISTINGS = f"{ABO_BASE}/archives/abo-listings.tar"
ABO_IMAGES_CSV = f"{ABO_BASE}/images/metadata/images.csv.gz"
ABO_LICENCE = f"{ABO_BASE}/LICENSE-CC-BY-4.0.txt"
GLAMI_ZIP = "https://huggingface.co/datasets/glami/glami-1m/resolve/main/GLAMI-1M-dataset.zip"
GLAMI_MEMBER = "GLAMI-1M-dataset/{}"
HUMAN_LABELS = {"admin", "quality-check", "custom-tag"}
PER_CATEGORY = 300
ABO_SHARE = 200  # of PER_CATEGORY for shoes, bags and accessories; GLAMI gives the rest
ABO_PHOTOS = 3  # front plus two other views
MAX_DESCRIPTION = 400


def _request(url: str, **headers: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})


def download(url: str, path: Path) -> Path:
    if path.is_file() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with (
        urllib.request.urlopen(_request(url), timeout=120) as response,
        temporary.open("wb") as stream,
    ):
        shutil.copyfileobj(response, stream, 1024 * 1024)
    temporary.replace(path)
    return path


class HttpRangeFile(io.RawIOBase):
    """A seekable read-only view of a remote file, so zipfile reads only what it needs."""

    def __init__(self, url: str):
        with urllib.request.urlopen(_request(url, Range="bytes=0-0"), timeout=60) as response:
            self.url = response.url  # the signed CDN location after redirects
            self.size = int(response.headers["Content-Range"].rsplit("/", 1)[1])
        self.position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = 0) -> int:
        self.position = {0: offset, 1: self.position + offset, 2: self.size + offset}[whence]
        return self.position

    def read_range(self, start: int, length: int) -> bytes:
        end = min(start + length, self.size) - 1
        request = _request(self.url, Range=f"bytes={start}-{end}")
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()

    def readinto(self, buffer) -> int:
        if self.position >= self.size:
            return 0
        data = self.read_range(self.position, len(buffer))
        buffer[: len(data)] = data
        self.position += len(data)
        return len(data)


def remote_member(remote: HttpRangeFile, info: zipfile.ZipInfo) -> bytes:
    """One stored or deflated zip member in a single range request."""
    head = 30 + len(info.filename.encode()) + 1024
    data = remote.read_range(info.header_offset, head + info.compress_size)
    if data[:4] != b"PK\x03\x04":
        raise ValueError(f"bad local header for {info.filename}")
    start = 30 + int.from_bytes(data[26:28], "little") + int.from_bytes(data[28:30], "little")
    payload = data[start : start + info.compress_size]
    if info.compress_type == zipfile.ZIP_STORED:
        return payload
    if info.compress_type == zipfile.ZIP_DEFLATED:
        import zlib

        return zlib.decompress(payload, -15)
    raise ValueError(f"unsupported compression for {info.filename}")


def _english(values: list[dict[str, Any]] | None) -> str | None:
    """The en_US value, else any English value."""
    english = [item for item in values or [] if item.get("language_tag", "").startswith("en")]
    english.sort(key=lambda item: item.get("language_tag") != "en_US")
    return str(english[0]["value"]).strip() if english else None


def _round_robin(groups: dict[str, list[Any]], count: int) -> list[Any]:
    """Take items from each subgroup in turn, so one large subclass cannot dominate."""
    queues = {key: list(items) for key, items in sorted(groups.items())}
    chosen: list[Any] = []
    while len(chosen) < count and any(queues.values()):
        for key in list(queues):
            if queues[key] and len(chosen) < count:
                chosen.append(queues[key].pop(0))
    return chosen


def _description(text: str | None, credit: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = " ".join(text.split())
    if len(text) > MAX_DESCRIPTION:
        text = text[: MAX_DESCRIPTION - 1].rsplit(" ", 1)[0] + "…"
    return f"{text} ({credit}; synthetic price.)" if text else f"{credit}; synthetic price."


def abo_candidates(raw: Path) -> dict[str, dict[str, list[PracticeProduct]]]:
    """ABO products by category and product type, deduplicated across sizes and stores."""
    listings = raw / "abo" / "abo-listings.tar"
    download(ABO_LISTINGS, listings)
    download(ABO_IMAGES_CSV, raw / "abo" / "images.csv.gz")
    download(ABO_LICENCE, raw / "abo" / "LICENSE-CC-BY-4.0.txt")
    with gzip.open(raw / "abo" / "images.csv.gz", "rt", encoding="utf-8") as stream:
        images = {
            row["image_id"]: row
            for row in csv.DictReader(stream)
            if min(int(row["height"]), int(row["width"])) >= 100
        }
    ranked: list[tuple[tuple, PracticeProduct]] = []
    with tarfile.open(listings) as archive:
        for member in archive.getmembers():
            if not member.name.endswith(".json.gz"):
                continue
            with gzip.open(archive.extractfile(member), "rt", encoding="utf-8") as stream:
                for line in stream:
                    row = json.loads(line)
                    product = _abo_product(row, images)
                    if product is not None:
                        # Prefer amazon.com rows (US English titles), then more photos.
                        rank = (row.get("domain_name") == "amazon.com", len(product.images))
                        ranked.append((rank, product))
    ranked.sort(key=lambda item: (item[0], item[1].practice_id), reverse=True)
    # Size variants share one photo set and stores repeat items: keep one product per
    # item, per main photo, and per model colour.
    seen: set[str] = set()
    unique: list[PracticeProduct] = []
    for _, product in ranked:
        keys = {
            product.practice_id,
            product.images[0].path,
            f"{product.group_id}|{product.colour or product.title}",
        }
        if not keys & seen:
            seen |= keys
            unique.append(product)
    # A photo shown for several products (gift box, size card) identifies none of them.
    shared = Counter(image.path for product in unique for image in product.images)
    grouped: dict[str, dict[str, list[PracticeProduct]]] = defaultdict(lambda: defaultdict(list))
    for product in unique:
        own = [image for image in product.images if shared[image.path] == 1][:ABO_PHOTOS]
        if len(own) >= 2 and own[0].view == "main":
            product = product.model_copy(update={"images": own})
            grouped[product.category][product.source_category].append(product)
    for subgroups in grouped.values():
        for items in subgroups.values():
            items.sort(key=lambda item: stable_fraction("pick", item.practice_id))
    return grouped


def _abo_product(row: dict[str, Any], images: dict[str, dict[str, str]]) -> PracticeProduct | None:
    product_type = (row.get("product_type") or [{}])[0].get("value")
    category = ABO_CATEGORIES.get(product_type)
    name = _english(row.get("item_name"))
    if category is None or not name:
        return None
    photo_ids = [row.get("main_image_id"), *row.get("other_image_id", [])]
    photos = [images[photo] for photo in dict.fromkeys(photo_ids) if photo in images][:8]
    if len(photos) < 2 or photo_ids[0] not in images:
        return None
    title = abo_title(name)
    brand = _english(row.get("brand"))
    colour_values = [
        value
        for item in row.get("color", [])
        if item.get("language_tag", "").startswith("en")
        for value in item.get("standardized_values") or [item.get("value")]
        if value
    ]
    colour = colour_in(" ".join(colour_values[:1])) or colour_in(title.split(",", 1)[-1])
    practice_id = f"abo-{row['item_id']}"
    bullets = " ".join(
        item["value"] for item in row.get("bullet_point", [])[:2] if item.get("value")
    )
    return PracticeProduct(
        practice_id=practice_id,
        dataset="abo",
        group_id="abo:" + hashlib.sha256(model_key(brand, title).encode()).hexdigest()[:16],
        title=title,
        language="en",
        brand=brand,
        category=category,
        source_category=product_type,
        gender=title_gender(title),
        colour=colour,
        colour_names=[colour.title()] if colour else colour_values[:1],
        description=_description(bullets, ABO_CREDIT),
        price_huf=synthetic_price(practice_id, category),
        sizes=synthetic_sizes(practice_id, category),
        images=[
            PracticeImage(
                url=f"{ABO_BASE}/images/small/{photo['path']}",
                path=f"images/abo/{Path(photo['path']).stem}.jpg",
                view="main" if number == 0 else "other",
            )
            for number, photo in enumerate(photos)
        ],
        credit=ABO_CREDIT,
    )


def glami_candidates(raw: Path, remote: HttpRangeFile | None) -> dict[str, dict[str, list]]:
    """Hungarian, human-labelled GLAMI rows by category and GLAMI class."""
    folder = raw / "glami" / "GLAMI-1M-dataset"
    for name in ("GLAMI-1M-test.csv", "GLAMI-1M-train.csv", "LICENSE", "NOTICE", "README.txt"):
        target = folder / name
        if not target.is_file():
            if remote is None:
                raise ValueError(f"missing {target}; run with network access")
            archive = zipfile.ZipFile(io.BufferedReader(remote, buffer_size=8 * 1024 * 1024))
            folder.mkdir(parents=True, exist_ok=True)
            with archive.open(GLAMI_MEMBER.format(name)) as source, target.open("wb") as out:
                shutil.copyfileobj(source, out, 8 * 1024 * 1024)
    csv.field_size_limit(10**8)
    grouped: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    seen_images: set[str] = set()
    seen_names: set[str] = set()
    for split in ("test", "train"):
        with (folder / f"GLAMI-1M-{split}.csv").open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                if row["geo"] != "hu" or row["label_source"] not in HUMAN_LABELS:
                    continue
                name = " ".join(row["name"].split())
                category = glami_category(row["category_name"], name)
                key = name.casefold()
                if (
                    category is None
                    or len(name) < 4
                    or row["image_id"] in seen_images
                    or key in seen_names
                ):
                    continue
                seen_images.add(row["image_id"])
                seen_names.add(key)
                practice_id = f"glami-{row['item_id']}"
                colour = colour_in(name)
                product = PracticeProduct(
                    practice_id=practice_id,
                    dataset="glami",
                    group_id=f"glami:{row['item_id']}",
                    title=name[:300],
                    language="hu",
                    category=category,
                    source_category=row["category_name"],
                    gender=glami_gender(row["category_name"]),
                    colour=colour,
                    colour_names=[colour.title()] if colour else [],
                    description=_description(row["description"], GLAMI_CREDIT),
                    price_huf=synthetic_price(practice_id, category),
                    sizes=synthetic_sizes(practice_id, category),
                    images=[
                        PracticeImage(
                            url=f"{LOCAL_IMAGE_PREFIX}glami/{row['image_id']}.jpg",
                            path=f"images/glami/{row['image_id']}.jpg",
                            view="main",
                        )
                    ],
                    credit=GLAMI_CREDIT,
                )
                grouped[category][row["category_name"]].append(product)
    for subgroups in grouped.values():
        for items in subgroups.values():
            items.sort(key=lambda item: stable_fraction("pick", item.practice_id))
    return grouped


def _valid_jpeg(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return image.format == "JPEG" and image.width * image.height <= 20_000_000
    except Exception:  # noqa: BLE001 - any undecodable photo is dropped
        return False


def fetch_photos(
    products: Iterable[PracticeProduct], out: Path, remote: HttpRangeFile | None
) -> set[str]:
    """Store every product photo under ``out``; return the paths that failed."""
    wanted = {image.path: image for product in products for image in product.images}
    members: dict[str, zipfile.ZipInfo] = {}
    if any(path.startswith("images/glami/") for path in wanted):
        if remote is None:
            raise ValueError("GLAMI photos need network access")
        archive = zipfile.ZipFile(io.BufferedReader(remote, buffer_size=8 * 1024 * 1024))
        members = {info.filename: info for info in archive.infolist()}

    def fetch(item: tuple[str, PracticeImage]) -> str | None:
        path, image = item
        target = out / path
        if target.is_file() and target.stat().st_size > 0:
            return None
        try:
            if path.startswith("images/glami/"):
                info = members[GLAMI_MEMBER.format("images/" + Path(path).name)]
                data = remote_member(remote, info)
            else:
                with urllib.request.urlopen(_request(image.url), timeout=60) as response:
                    data = response.read(5 * 1024 * 1024 + 1)
        except (KeyError, OSError, ValueError):
            return path
        if len(data) > 5 * 1024 * 1024 or not _valid_jpeg(data):
            return path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        temporary.write_bytes(data)
        temporary.replace(target)
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return {path for path in pool.map(fetch, wanted.items()) if path}


def build(out: Path, *, per_category: int = PER_CATEGORY, abo_share: int = ABO_SHARE) -> dict:
    out = out.resolve()
    if (Path(__file__).resolve().parents[3] / ".git").exists() and out.is_relative_to(
        Path(__file__).resolve().parents[3]
    ):
        raise ValueError("write the practice catalogue outside the repository")
    raw = out / "raw"
    remote = HttpRangeFile(GLAMI_ZIP)
    abo = abo_candidates(raw)
    glami = glami_candidates(raw, remote)
    chosen: list[PracticeProduct] = []
    for category in sorted(CATEGORIES):
        from_abo = abo_share if category in abo else 0
        picks = _round_robin(abo.get(category, {}), round(from_abo * 1.1))
        picks += _round_robin(glami.get(category, {}), round((per_category - from_abo) * 1.1))
        chosen.extend(picks)
    failed = fetch_photos(chosen, out, remote)
    products: list[PracticeProduct] = []
    taken: Counter[tuple[str, str]] = Counter()
    for product in chosen:
        images = [image for image in product.images if image.path not in failed]
        minimum = 2 if product.dataset == "abo" else 1
        limit = (
            abo_share
            if product.dataset == "abo"
            else per_category - (abo_share if product.category in abo else 0)
        )
        if len(images) < minimum or taken[(product.category, product.dataset)] >= limit:
            continue
        taken[(product.category, product.dataset)] += 1
        products.append(product.model_copy(update={"images": images}))
    # Photos of oversampled candidates that were not kept are removed.
    kept = {image.path for product in products for image in product.images}
    for path in (out / "images").glob("*/*.jpg"):
        if path.relative_to(out).as_posix() not in kept:
            path.unlink()
    temporary = out / (CATALOGUE_FILE + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for product in products:
            stream.write(product.model_dump_json() + "\n")
    temporary.replace(out / CATALOGUE_FILE)
    for source, name in (
        (raw / "abo" / "LICENSE-CC-BY-4.0.txt", "LICENSE-ABO-CC-BY-4.0.txt"),
        (raw / "glami" / "GLAMI-1M-dataset" / "LICENSE", "LICENSE-GLAMI-Apache-2.0.txt"),
        (raw / "glami" / "GLAMI-1M-dataset" / "NOTICE", "NOTICE-GLAMI.txt"),
    ):
        shutil.copyfile(source, out / name)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"abo": 0, "glami": 0})
    for (category, dataset), number in taken.items():
        counts[category][dataset] = number
    info = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "products": len(products),
        "photos": sum(len(product.images) for product in products),
        "products_by_category": {key: counts[key] for key in sorted(counts)},
        "failed_photos": len(failed),
        "sources": {"abo": [ABO_LISTINGS, ABO_IMAGES_CSV], "glami": [GLAMI_ZIP]},
    }
    (out / "build-info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--per-category", type=int, default=PER_CATEGORY)
    args = parser.parse_args(argv)
    try:
        result = build(args.out, per_category=args.per_category)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

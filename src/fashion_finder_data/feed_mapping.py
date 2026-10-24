"""Map a local partner XML export to the canonical timestamped feed CSV.

Affiliate exports (CJ, Tradedoubler, Dognet; often Google Shopping fields) need a
few things a one-path-per-field map cannot say. The mapping file therefore allows:

- ordered fallback paths: ``"price": ["sale_price", "price"]`` takes the first
  non-empty one, and ``"original_price": ["price"]`` keeps the regular price
  (normalisation drops it when it is not above the current price);
- ``additional_image_urls``: every matching element, comma lists split;
- ``item_group_id``: consecutive rows of one group on one product URL (size
  variants) become one row with all sizes, so they do not collide in storage;
- ``filter``: ``{"path": "google_product_category", "pattern": "Clothing"}`` keeps
  only matching records, so a 500,000-product export can be cut to one category;
- streaming parsing of large or gzip-compressed XML, and ``allow_partial`` to write
  the valid rows and report the rest instead of failing the whole feed.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import iterparse

from .feed import validate_feed
from .normalisation import normalise_availability, parse_price_text
from .sources import SOURCES

MAX_XML_BYTES = 512 * 1024 * 1024  # streamed, so memory stays flat
MAX_FALLBACKS = 5

CANONICAL_COLUMNS = (
    "product_url",
    "name",
    "price",
    "currency",
    "observed_at",
    "sku",
    "brand",
    "category",
    "description",
    "original_price",
    "image_url",
    "colour",
    "size",
    "availability",
    "item_group_id",
    "additional_image_urls",
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")


def _check_path(key: str, path_value: Any) -> None:
    if not isinstance(path_value, str):
        raise TypeError("unknown canonical field or non-string XML path")
    segments = path_value.split("/")
    if not segments or any(
        not _NAME.fullmatch(segment.removeprefix("@"))
        or (segment.startswith("@") and index != len(segments) - 1)
        for index, segment in enumerate(segments)
    ):
        raise ValueError(f"invalid XML path for {key}")


def _mapping(path: Path) -> tuple[str, dict[str, list[str]], dict[str, str], dict | None]:
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    allowed = {"record_tag", "fields", "constants", "filter"}
    if not isinstance(document, dict) or set(document) - allowed:
        raise ValueError("mapping must contain only record_tag, fields, constants and filter")
    record_tag = document.get("record_tag")
    fields = document.get("fields")
    constants = document.get("constants", {})
    if not isinstance(record_tag, str) or not _NAME.fullmatch(record_tag):
        raise ValueError("record_tag must be one XML element name")
    if not isinstance(fields, dict) or not isinstance(constants, dict):
        raise TypeError("fields and constants must be objects")
    if set(fields) & set(constants):
        raise ValueError("a canonical field cannot be mapped and constant")
    if not {"product_url", "name", "price", "currency"}.issubset(fields.keys() | constants.keys()):
        raise ValueError("mapping needs product_url, name, price and currency")
    paths: dict[str, list[str]] = {}
    for key, value in fields.items():
        if key not in CANONICAL_COLUMNS:
            raise ValueError("unknown canonical field or non-string XML path")
        options = value if isinstance(value, list) else [value]
        if not 1 <= len(options) <= MAX_FALLBACKS:
            raise ValueError(f"{key}: give 1–{MAX_FALLBACKS} fallback paths")
        for option in options:
            _check_path(key, option)
        paths[key] = options
    for key, value in constants.items():
        if key not in CANONICAL_COLUMNS or not isinstance(value, str):
            raise ValueError("unknown canonical field or non-string constant")
    selector = document.get("filter")
    if selector is not None:
        if not isinstance(selector, dict) or set(selector) != {"path", "pattern"}:
            raise ValueError("filter needs exactly a path and a pattern")
        _check_path("filter", selector["path"])
        re.compile(selector["pattern"])
    return record_tag, paths, constants, selector


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _values(element: Any, path: str) -> list[str]:
    """Every value a path reaches (``a/b`` may match several ``b`` children)."""
    current = [element]
    for segment in path.split("/"):
        if segment.startswith("@"):
            return [
                (item.attrib.get(segment[1:]) or "").strip()
                for item in current
                if item.attrib.get(segment[1:])
            ]
        current = [child for item in current for child in item if _local_name(child.tag) == segment]
    return [text for item in current if (text := "".join(item.itertext()).strip())]


def _value(element: Any, path: str | list[str]) -> str:
    for option in path if isinstance(path, list) else [path]:
        found = _values(element, option)
        if found:
            return found[0]
    return ""


def _row(product: Any, fields: dict[str, list[str]], constants: dict[str, str]) -> dict[str, str]:
    row = {}
    for key, options in fields.items():
        if key == "additional_image_urls":
            urls: list[str] = []
            for option in options:
                for value in _values(product, option):
                    urls += [part.strip() for part in value.split(",") if part.strip()]
            row[key] = " ".join(dict.fromkeys(urls))
        else:
            row[key] = _value(product, options)
    row.update(constants)
    return row


def _merge(group: list[dict[str, str]]) -> tuple[dict[str, str] | None, str | None]:
    """One row for the size variants of one product URL, or why they cannot merge."""
    if len(group) == 1:
        return group[0], None
    prices = set()
    in_stock_prices = set()
    for row in group:
        try:
            amount = parse_price_text(row.get("price", ""), row.get("currency") or None)[0]
        except (TypeError, ValueError):
            return None, "variant has an invalid price"
        prices.add(amount)
        if normalise_availability(row.get("availability")) == "in_stock":
            in_stock_prices.add(amount)
    chosen = prices if len(prices) == 1 else in_stock_prices
    if len(chosen) != 1:
        return None, "variants of one product URL have different prices"
    price = chosen.pop()
    base = next(
        row
        for row in group
        if parse_price_text(row["price"], row.get("currency") or None)[0] == price
    )
    sizes = [row.get("size", "").strip() for row in group if row.get("size", "").strip()]
    states = [normalise_availability(row.get("availability")) for row in group]
    merged = dict(base)
    merged["size"] = "|".join(dict.fromkeys(sizes))
    merged["availability"] = (
        "in_stock"
        if "in_stock" in states
        else "out_of_stock"
        if all(state == "out_of_stock" for state in states)
        else ""
    )
    merged["sku"] = base.get("item_group_id") or base.get("sku", "")
    return merged, None


def _records(xml_path: Path, record_tag: str):
    with xml_path.open("rb") as probe:
        compressed = probe.read(2) == b"\x1f\x8b"
    opener = gzip.open if compressed else open
    with opener(xml_path, "rb") as stream:
        for _, element in iterparse(stream, events=("end",), forbid_dtd=True):
            if _local_name(element.tag) == record_tag:
                yield element
                element.clear()


def map_xml_feed(
    source_key: str,
    xml_path: Path,
    mapping_path: Path,
    output_path: Path,
    *,
    observed_at: str | None = None,
    allow_partial: bool = False,
) -> dict:
    """Write a new validated CSV atomically; leave an existing output untouched.

    With ``allow_partial`` the valid rows are written and the rejected ones counted
    (the report lists the first reasons); otherwise any invalid row fails the feed.
    """
    if source_key not in SOURCES:
        raise ValueError("unknown source")
    if output_path.exists():
        raise FileExistsError(f"output already exists: {output_path}")
    if xml_path.stat().st_size > MAX_XML_BYTES:
        raise ValueError("XML feed exceeds the 512 MB limit")
    record_tag, fields, constants, selector = _mapping(mapping_path)
    if observed_at is not None and "observed_at" in fields | constants:
        raise ValueError("observed_at is specified twice")
    if observed_at is None and "observed_at" not in fields | constants:
        raise ValueError("supply observed_at in the mapping or --observed-at")
    if observed_at is not None:
        constants["observed_at"] = observed_at
    pattern = re.compile(selector["pattern"]) if selector else None
    grouping = "item_group_id" in fields
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    stats: Counter[str] = Counter()
    merge_errors: list[str] = []
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=CANONICAL_COLUMNS)
            writer.writeheader()
            group: list[dict[str, str]] = []

            def flush() -> None:
                if not group:
                    return
                merged, problem = _merge(group)
                if merged is None:
                    stats["groups_rejected"] += 1
                    merge_errors.append(f"group {group[0].get('item_group_id')}: {problem}")
                else:
                    writer.writerow(merged)
                    stats["rows_merged"] += len(group) - 1
                group.clear()

            try:
                for product in _records(xml_path, record_tag):
                    stats["records"] += 1
                    if pattern is not None and not pattern.search(
                        _value(product, selector["path"])
                    ):
                        stats["filtered_out"] += 1
                        continue
                    row = _row(product, fields, constants)
                    key = (row.get("item_group_id"), row.get("product_url"))
                    if grouping and row.get("item_group_id"):
                        if (
                            group
                            and (group[0].get("item_group_id"), group[0].get("product_url")) != key
                        ):
                            flush()
                        group.append(row)
                        continue
                    flush()
                    writer.writerow(row)
                flush()
            except (DefusedXmlException, ParseError) as exc:
                raise ValueError(
                    "XML input is malformed or contains forbidden declarations"
                ) from exc
        result = validate_feed(temporary, source_key)
        if result["status"] != "valid" or merge_errors:
            if not allow_partial or result["records_valid"] == 0:
                summary = "; ".join((merge_errors + result["errors"])[:3]) or "no valid products"
                raise ValueError(f"mapped feed is invalid: {summary}")
            temporary = _keep_valid_rows(temporary, source_key, output_path)
            result = {
                **validate_feed(temporary, source_key),
                "rows_rejected_before_output": result["records_rejected"] + len(merge_errors),
                "errors": (merge_errors + result["errors"])[:20],
            }
        os.link(temporary, output_path)
        return {**result, **dict(stats), "output": str(output_path)}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _keep_valid_rows(path: Path, source_key: str, output_path: Path) -> Path:
    """Rewrite a mapped CSV with only the rows the validator accepts."""
    from .feed import row_problems

    rejected = row_problems(path, source_key)
    with (
        tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{output_path.name}.",
            suffix=".valid.tmp",
            dir=output_path.parent,
            delete=False,
        ) as cleaned,
        path.open(encoding="utf-8", newline="") as source,
    ):
        reader = csv.DictReader(source)
        writer = csv.DictWriter(cleaned, fieldnames=CANONICAL_COLUMNS)
        writer.writeheader()
        for number, row in enumerate(reader, 2):
            if number not in rejected:
                writer.writerow(row)
    path.unlink(missing_ok=True)
    return Path(cleaned.name)

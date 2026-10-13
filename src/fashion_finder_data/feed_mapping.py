"""Map a local partner XML export to the canonical timestamped feed CSV."""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import DefusedXMLParser, parse

from .feed import MAX_FEED_BYTES, validate_feed
from .sources import SOURCES

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
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")


def _mapping(path: Path) -> tuple[str, dict[str, str], dict[str, str]]:
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) - {"record_tag", "fields", "constants"}:
        raise ValueError("mapping must contain only record_tag, fields and constants")
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
    for key, path_value in fields.items():
        if key not in CANONICAL_COLUMNS or not isinstance(path_value, str):
            raise ValueError("unknown canonical field or non-string XML path")
        segments = path_value.split("/")
        if not segments or any(
            not _NAME.fullmatch(segment.removeprefix("@"))
            or (segment.startswith("@") and index != len(segments) - 1)
            for index, segment in enumerate(segments)
        ):
            raise ValueError(f"invalid XML path for {key}")
    for key, value in constants.items():
        if key not in CANONICAL_COLUMNS or not isinstance(value, str):
            raise ValueError("unknown canonical field or non-string constant")
    return record_tag, fields, constants


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _value(element: Any, path: str) -> str:
    current = element
    for segment in path.split("/"):
        if segment.startswith("@"):
            return (current.attrib.get(segment[1:]) or "").strip()
        current = next((child for child in current if _local_name(child.tag) == segment), None)
        if current is None:
            return ""
    return "".join(current.itertext()).strip()


def map_xml_feed(
    source_key: str,
    xml_path: Path,
    mapping_path: Path,
    output_path: Path,
    *,
    observed_at: str | None = None,
) -> dict:
    """Write a new validated CSV atomically; leave an existing output untouched."""
    if source_key not in SOURCES:
        raise ValueError("unknown source")
    if output_path.exists():
        raise FileExistsError(f"output already exists: {output_path}")
    if xml_path.stat().st_size > MAX_FEED_BYTES:
        raise ValueError("XML feed exceeds the 20 MB limit")
    record_tag, fields, constants = _mapping(mapping_path)
    if observed_at is not None and "observed_at" in fields | constants:
        raise ValueError("observed_at is specified twice")
    if observed_at is None and "observed_at" not in fields | constants:
        raise ValueError("supply observed_at in the mapping or --observed-at")
    if observed_at is not None:
        constants["observed_at"] = observed_at

    try:
        tree = parse(xml_path, parser=DefusedXMLParser(forbid_dtd=True))
    except (DefusedXmlException, ParseError) as exc:
        raise ValueError("XML input is malformed or contains forbidden declarations") from exc
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
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
            for product in tree.iter():
                if _local_name(product.tag) != record_tag:
                    continue
                row = {key: _value(product, field_path) for key, field_path in fields.items()}
                row.update(constants)
                writer.writerow(row)
        result = validate_feed(temporary, source_key)
        if result["status"] != "valid":
            summary = "; ".join(result["errors"][:3]) or "no valid products"
            raise ValueError(f"mapped feed is invalid: {summary}")
        os.link(temporary, output_path)
        return {**result, "output": str(output_path)}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

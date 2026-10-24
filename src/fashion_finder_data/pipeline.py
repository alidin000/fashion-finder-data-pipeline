"""Raw archive, validation, persistence, and run reporting."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from django.apps import apps
from pydantic import ValidationError

from .schema import RawProductRecord, RetailerOffer, canonical_url, normalize, utc_now
from .storage import Database, save_run, upsert_offer

# Columns of the CollectionRun table; extra report keys go to the JSON report only.
RUN_FIELDS = (
    "run_id",
    "source_key",
    "market_code",
    "started_at",
    "finished_at",
    "status",
    "records_discovered",
    "records_parsed",
    "records_seen",
    "records_valid",
    "records_rejected",
    "records_upserted",
    "records_deduplicated",
    "error_summary",
    "completeness",
    "adapter_version",
)
COVERAGE_FIELDS = (
    "title",
    "price",
    "currency",
    "original_price",
    "image",
    "brand",
    "description",
    "colour",
    "size",
    "sku",
    "known_availability",
    "mapped_category",
)
OfferHook = Callable[[RawProductRecord, RetailerOffer, str], None]


def rejection_reason(exc: Exception) -> str:
    """A short, stable reason for the run report (no product data, no URLs)."""
    if isinstance(exc, ValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ())) or "record"
        return f"{where}: {first.get('msg', 'invalid')}"[:120]
    text = str(exc).split("\n", 1)[0]
    return (text or type(exc).__name__)[:120]


def check_offer(offer: RetailerOffer) -> None:
    """Pipeline-level rules beyond the schema: a listed price must be positive."""
    if offer.price_amount is not None and offer.price_amount <= 0:
        raise ValueError("price must be greater than zero")


def field_coverage(offers: list[RetailerOffer]) -> dict[str, float]:
    present = Counter()
    for offer in offers:
        present["title"] += bool(offer.title)
        present["price"] += offer.price_amount is not None
        present["currency"] += offer.currency is not None
        present["original_price"] += offer.original_price_amount is not None
        present["image"] += bool(offer.image_urls)
        present["brand"] += bool(offer.brand)
        present["description"] += bool(offer.description)
        present["colour"] += bool(offer.colour_names)
        present["size"] += bool(offer.size_options)
        present["sku"] += bool(offer.source_product_id)
        present["known_availability"] += offer.availability_status != "unknown"
        present["mapped_category"] += offer.category is not None
    total = len(offers)
    return {key: round(present[key] / total, 3) if total else 0.0 for key in COVERAGE_FIELDS}


def _exists(engine: Database, offer: RetailerOffer) -> bool:
    Offer = apps.get_model("catalogue", "RetailerOffer")
    query = Offer.objects.using(engine.alias).filter(
        retailer__source_key=offer.source_key, retailer__market_code=offer.market_code
    )
    if offer.source_product_id and query.filter(source_product_id=offer.source_product_id).exists():
        return True
    return query.filter(canonical_url=offer.canonical_url).exists()


def _dedupe_key(record: RawProductRecord) -> str | None:
    product = record.product
    if product.get("sku"):
        return f"sku:{str(product['sku']).strip()}"
    try:
        return "url:" + canonical_url(str(product.get("url") or record.source_url))
    except ValueError:
        return None


def archive_raw(root: Path, source_key: str, run_id: str, records: list[RawProductRecord]) -> Path:
    path = root / "raw" / source_key / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return path


def ingest_records(
    engine: Database,
    root: Path,
    source_key: str,
    records: list[RawProductRecord],
    collection_errors: list[str] | None = None,
    run_id: str | None = None,
    started_at: str | None = None,
    archive: bool = True,
    discovered: int | None = None,
    preparse_rejected: int = 0,
    on_offer: OfferHook | None = None,
) -> dict:
    """Validate, de-duplicate and upsert one source's records, then write its run report.

    Upserts are idempotent, so replaying a resumed run's records never duplicates an
    offer. ``on_offer`` sees each stored record with ``new``, ``changed`` or
    ``unchanged``.
    """
    run_id = run_id or uuid4().hex
    started_at = started_at or utc_now().isoformat()
    if archive:
        archive_raw(root, source_key, run_id, records)
    errors = Counter(collection_errors or [])
    reasons: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    valid_offers: list[RetailerOffer] = []
    rejected = 0
    upserted = 0
    deduplicated = 0
    duplicates = 0
    seen_keys: set[str] = set()
    completeness = Counter()
    for record in records:
        key = _dedupe_key(record)
        if key is not None and key in seen_keys:
            duplicates += 1
            continue
        try:
            if record.source_key != source_key:
                raise ValueError("record source does not match run source")
            offer = normalize(record)
            check_offer(offer)
            existed = _exists(engine, offer)
            changed = upsert_offer(engine, offer)
        except (ValueError, TypeError, ValidationError) as exc:
            rejected += 1
            errors[f"validation: {type(exc).__name__}"] += 1
            reasons[rejection_reason(exc)] += 1
            continue
        if key is not None:
            seen_keys.add(key)
        valid_offers.append(offer)
        upserted += int(changed)
        deduplicated += int(not changed)
        outcome = "unchanged" if not changed else "changed" if existed else "new"
        outcomes[outcome] += 1
        if on_offer is not None:
            on_offer(record, offer, outcome)
        for field, present in {
            "price": offer.price_amount is not None,
            "currency": offer.currency is not None,
            "image_url": bool(offer.image_urls),
            "product_url": bool(offer.source_url),
        }.items():
            completeness[field] += int(present)
    valid = len(valid_offers)
    report = {
        "run_id": run_id,
        "source_key": source_key,
        "market_code": "HU",
        "started_at": started_at,
        "finished_at": utc_now().isoformat(),
        "status": "success" if valid and not errors else "partial" if valid else "failed",
        "records_discovered": discovered if discovered is not None else len(records),
        "records_parsed": len(records),
        "records_seen": len(records) + preparse_rejected,
        "records_valid": valid,
        "records_rejected": rejected + preparse_rejected,
        "records_upserted": upserted,
        "records_deduplicated": deduplicated + duplicates,
        "error_summary": dict(errors),
        "completeness": {
            key: round(completeness[key] / valid, 3) if valid else 0.0
            for key in ("price", "currency", "image_url", "product_url")
        },
        "adapter_version": (
            records[0].adapter_version
            if records
            and all(item.adapter_version == records[0].adapter_version for item in records)
            else "mixed"
            if records
            else "unknown"
        ),
        "offers": {key: outcomes[key] for key in ("new", "changed", "unchanged")},
        "duplicates_in_run": duplicates,
        "field_coverage": field_coverage(valid_offers),
        "rejection_reasons": dict(reasons.most_common()),
    }
    save_run(engine, {key: report[key] for key in RUN_FIELDS})
    write_report(root, report)
    return report


def write_report(root: Path, report: dict) -> Path:
    report_path = root / "reports" / f"{report['run_id']}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_name(f".{report_path.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    temporary.replace(report_path)
    return report_path


def ingest_jsonl(engine: Database, root: Path, path: Path, source_key: str) -> dict:
    records: list[RawProductRecord] = []
    errors: list[str] = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                records.append(RawProductRecord.model_validate_json(line))
            except (ValueError, ValidationError) as exc:
                errors.append(f"raw line {number}: {type(exc).__name__}")
    return ingest_records(
        engine,
        root,
        source_key,
        records,
        errors,
        discovered=len(records) + len(errors),
        preparse_rejected=len(errors),
    )

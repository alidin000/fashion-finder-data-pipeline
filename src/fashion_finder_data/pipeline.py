"""Raw archive, validation, persistence, and run reporting."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .schema import RawProductRecord, normalize, utc_now
from .storage import Database, save_run, upsert_offer


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
) -> dict:
    run_id = run_id or uuid4().hex
    started_at = started_at or utc_now().isoformat()
    if archive:
        archive_raw(root, source_key, run_id, records)
    errors = Counter(collection_errors or [])
    valid = 0
    rejected = 0
    upserted = 0
    deduplicated = 0
    completeness = Counter()
    for record in records:
        try:
            if record.source_key != source_key:
                raise ValueError("record source does not match run source")
            offer = normalize(record)
            changed = upsert_offer(engine, offer)
        except (ValueError, TypeError, ValidationError) as exc:
            rejected += 1
            errors[f"validation: {type(exc).__name__}"] += 1
            continue
        valid += 1
        upserted += int(changed)
        deduplicated += int(not changed)
        for field, present in {
            "price": offer.price_amount is not None,
            "currency": offer.currency is not None,
            "image_url": bool(offer.image_urls),
            "product_url": bool(offer.source_url),
        }.items():
            completeness[field] += int(present)
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
        "records_deduplicated": deduplicated,
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
    }
    save_run(engine, report)
    report_path = root / "reports" / f"{run_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


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

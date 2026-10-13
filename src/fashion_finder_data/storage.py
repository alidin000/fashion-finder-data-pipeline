"""Django-backed local catalogue storage and portable exports."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import django
from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import connections, transaction
from django.db.models import Q

from .schema import RetailerOffer as ParsedOffer
from .taxonomy import normalize_category


@dataclass(frozen=True)
class Database:
    alias: str
    path: Path


def _models():
    return (
        apps.get_model("catalogue", "Retailer"),
        apps.get_model("catalogue", "RetailerOffer"),
        apps.get_model("catalogue", "PriceObservation"),
        apps.get_model("catalogue", "CollectionRun"),
    )


def _upgrade_legacy_columns(path: Path) -> None:
    """Apply the two pre-Django additive upgrades before fake-initial migration."""
    if not path.exists():
        return
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "retailer_offer" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(retailer_offer)")}
            if "source_category" not in columns:
                connection.execute("ALTER TABLE retailer_offer ADD COLUMN source_category TEXT")
                for offer_id, category in connection.execute(
                    "SELECT id, category FROM retailer_offer WHERE category IS NOT NULL"
                ).fetchall():
                    connection.execute(
                        "UPDATE retailer_offer SET source_category=?, category=? WHERE id=?",
                        (category, normalize_category(category), offer_id),
                    )
        if "collection_run" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(collection_run)")}
            for column in ("records_discovered", "records_parsed"):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE collection_run ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                    )


def open_database(path: Path) -> Database:
    """Register a SQLite catalogue and apply Django migrations, including legacy adoption."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fashion_finder_data.django_project.settings")
    if not apps.ready:
        django.setup()
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    alias = "catalogue_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    if alias not in connections.databases:
        connections.databases[alias] = {**settings.DATABASES["default"], "NAME": str(path)}
    connections[alias].close()
    _upgrade_legacy_columns(path)
    call_command("migrate", database=alias, fake_initial=True, interactive=False, verbosity=0)
    return Database(alias, path)


def _iso(value: datetime) -> str:
    """Store aware times in UTC so stored timestamps sort and compare as text."""
    return (value.astimezone(UTC) if value.tzinfo else value).isoformat()


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# A missing value in a new record keeps the stored one: a feed row without photos
# must not wipe crawled photos, and "unknown" stock must not hide a known state.
KEEP_WHEN_MISSING = ("brand", "description", "image_urls", "colour_names", "size_options")


def _money_text(value: Decimal | None) -> str | None:
    return format(value.normalize(), "f") if value is not None else None


def offer_rows(
    database: Database, *, predicate: Q | None = None, **filters: Any
) -> list[dict[str, Any]]:
    """Return joined rows after database-side filtering."""
    _, Offer, _, _ = _models()
    query = Offer.objects.using(database.alias).select_related("retailer").filter(**filters)
    if predicate is not None:
        query = query.filter(predicate)
    rows = []
    for offer in query:
        row = {
            field.name: getattr(offer, field.name)
            for field in Offer._meta.fields
            if field.name != "retailer"
        }
        row["retailer_id"] = offer.retailer_id
        row["source_key"] = offer.retailer.source_key
        row["market_code"] = offer.retailer.market_code
        rows.append(row)
    return rows


def upsert_offer(database: Database, offer: ParsedOffer) -> bool:
    """Persist one offer; return True for a new offer or changed fields."""
    Retailer, Offer, Observation, _ = _models()
    now = _iso(offer.source_fetched_at)
    with transaction.atomic(using=database.alias):
        retailer, _ = Retailer.objects.using(database.alias).get_or_create(
            source_key=offer.source_key,
            market_code=offer.market_code,
            defaults={"name": offer.source_key.title(), "enabled": True},
        )
        offers = Offer.objects.using(database.alias)
        id_match = (
            offers.filter(retailer=retailer, source_product_id=offer.source_product_id).first()
            if offer.source_product_id
            else None
        )
        url_match = offers.filter(retailer=retailer, canonical_url=offer.canonical_url).first()
        if id_match and url_match and id_match.pk != url_match.pk:
            raise ValueError("source ID and canonical URL identify different existing offers")
        existing = id_match or url_match
        if (
            existing
            and existing.source_product_id
            and offer.source_product_id
            and existing.source_product_id != offer.source_product_id
        ):
            raise ValueError("canonical URL already belongs to a different source product ID")
        if existing and _instant(existing.source_fetched_at) > _instant(now):
            # An older observation arriving late only fills in price history.
            _record_price(Observation, database, existing.pk, offer, now)
            return False
        values = {
            "source_product_id": offer.source_product_id
            or (existing.source_product_id if existing else None),
            "source_url": offer.source_url,
            "canonical_url": offer.canonical_url,
            "title": offer.title,
            "brand": offer.brand,
            "category": offer.category,
            "source_category": offer.source_category,
            "description": offer.description,
            "price_amount": _money_text(offer.price_amount),
            "currency": offer.currency,
            "original_price_amount": _money_text(offer.original_price_amount),
            "image_urls": json.dumps(offer.image_urls, ensure_ascii=False),
            "colour_names": json.dumps(offer.colour_names, ensure_ascii=False),
            "size_options": json.dumps(offer.size_options, ensure_ascii=False),
            "availability_status": offer.availability_status,
            "source_fetched_at": now,
            "updated_at": now,
            "last_seen_at": now,
            "status": "active",
        }
        if existing and offer.price_amount is None:
            for key in ("price_amount", "currency", "original_price_amount"):
                values[key] = getattr(existing, key)
        if existing and offer.source_category is None:
            for key in ("category", "source_category"):
                values[key] = getattr(existing, key)
        if existing:
            for key in KEEP_WHEN_MISSING:
                if values[key] in (None, "[]"):
                    values[key] = getattr(existing, key)
            if offer.availability_status == "unknown":
                values["availability_status"] = existing.availability_status

            def differs(key: str, value: Any) -> bool:
                old = getattr(existing, key)
                if (
                    key in {"price_amount", "original_price_amount"}
                    and old is not None
                    and value is not None
                ):
                    return Decimal(old) != Decimal(value)
                return old != value

            changed = any(
                differs(key, value)
                for key, value in values.items()
                if key not in {"updated_at", "last_seen_at", "source_fetched_at"}
            )
            for key, value in values.items():
                setattr(existing, key, value)
            existing.save(using=database.alias)
            offer_id = existing.pk
        else:
            changed = True
            item = Offer.objects.using(database.alias).create(
                retailer=retailer, created_at=now, **values
            )
            offer_id = item.pk
        _record_price(Observation, database, offer_id, offer, now)
        return changed


def _record_price(Observation, database: Database, offer_id: int, offer: ParsedOffer, now: str):
    """Add a price observation when it differs from the one just before it in time."""
    if offer.price_amount is None or offer.currency is None:
        return
    observed = _instant(now)
    previous = None
    for candidate in Observation.objects.using(database.alias).filter(offer_id=offer_id):
        when = _instant(candidate.observed_at)
        if when <= observed and (previous is None or when >= _instant(previous.observed_at)):
            previous = candidate
    if (
        previous is None
        or Decimal(previous.price_amount) != offer.price_amount
        or previous.currency != offer.currency
    ):
        Observation.objects.using(database.alias).create(
            offer_id=offer_id,
            price_amount=_money_text(offer.price_amount),
            currency=offer.currency,
            observed_at=now,
            source_fetched_at=now,
        )


def save_run(database: Database, report: dict[str, Any]) -> None:
    _, _, _, Run = _models()
    values = dict(report)
    values["error_summary"] = json.dumps(values["error_summary"], ensure_ascii=False)
    values["completeness"] = json.dumps(values["completeness"], ensure_ascii=False)
    Run.objects.using(database.alias).create(**values)


def load_run(database: Database, run_id: str) -> dict[str, Any] | None:
    _, _, _, Run = _models()
    row = Run.objects.using(database.alias).filter(run_id=run_id).values().first()
    if row is not None:
        row["error_summary"] = json.loads(row["error_summary"])
        row["completeness"] = json.loads(row["completeness"])
    return row


def export_catalogue(database: Database, output: Path, source_key: str | None = None) -> int:
    rows = offer_rows(database)
    if source_key:
        rows = [row for row in rows if row["source_key"] == source_key]
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "source_key",
        "market_code",
        "source_product_id",
        "source_url",
        "title",
        "brand",
        "category",
        "source_category",
        "description",
        "price_amount",
        "currency",
        "original_price_amount",
        "image_urls",
        "colour_names",
        "size_options",
        "availability_status",
        "last_seen_at",
    ]
    if output.suffix.lower() == ".csv":

        def safe_cell(value: Any) -> Any:
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                return "'" + value
            return value

        with output.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows({key: safe_cell(row[key]) for key in fields} for row in rows)
    elif output.suffix.lower() == ".jsonl":
        with output.open("w", encoding="utf-8") as stream:
            for row in rows:
                item = {key: row[key] for key in fields}
                for key in ("image_urls", "colour_names", "size_options"):
                    item[key] = json.loads(item[key])
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    else:
        raise ValueError("export path must end in .csv or .jsonl")
    return len(rows)

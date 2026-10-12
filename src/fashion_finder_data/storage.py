"""Local SQLite storage and portable catalogue exports."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.engine import Engine

from .schema import RetailerOffer

metadata = MetaData()

retailers = Table(
    "retailer",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("source_key", String, nullable=False),
    Column("market_code", String, nullable=False),
    Column("name", String, nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    UniqueConstraint("source_key", "market_code"),
)

offers = Table(
    "retailer_offer",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("retailer_id", ForeignKey("retailer.id"), nullable=False),
    Column("source_product_id", String),
    Column("source_url", Text, nullable=False),
    Column("canonical_url", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("brand", String),
    Column("category", String),
    Column("description", Text),
    Column("price_amount", String),
    Column("currency", String),
    Column("original_price_amount", String),
    Column("image_urls", Text, nullable=False),
    Column("colour_names", Text, nullable=False),
    Column("size_options", Text, nullable=False),
    Column("availability_status", String, nullable=False),
    Column("source_fetched_at", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
    Column("status", String, nullable=False, default="active"),
    UniqueConstraint("retailer_id", "source_product_id"),
    UniqueConstraint("retailer_id", "canonical_url"),
)

observations = Table(
    "price_observation",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("offer_id", ForeignKey("retailer_offer.id"), nullable=False),
    Column("price_amount", String, nullable=False),
    Column("currency", String, nullable=False),
    Column("observed_at", String, nullable=False),
    Column("source_fetched_at", String, nullable=False),
)

runs = Table(
    "collection_run",
    metadata,
    Column("run_id", String, primary_key=True),
    Column("source_key", String, nullable=False),
    Column("market_code", String, nullable=False),
    Column("started_at", String, nullable=False),
    Column("finished_at", String, nullable=False),
    Column("status", String, nullable=False),
    Column("records_discovered", Integer, nullable=False),
    Column("records_parsed", Integer, nullable=False),
    Column("records_seen", Integer, nullable=False),
    Column("records_valid", Integer, nullable=False),
    Column("records_rejected", Integer, nullable=False),
    Column("records_upserted", Integer, nullable=False),
    Column("records_deduplicated", Integer, nullable=False),
    Column("error_summary", Text, nullable=False),
    Column("completeness", Text, nullable=False),
    Column("adapter_version", String, nullable=False),
)


def open_database(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    metadata.create_all(engine)
    with engine.begin() as connection:
        columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(collection_run)")
        }
        for name in ("records_discovered", "records_parsed"):
            if name not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE collection_run ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )
    return engine


def _iso(value: datetime) -> str:
    return value.isoformat()


def _money_text(value: Decimal | None) -> str | None:
    return format(value.normalize(), "f") if value is not None else None


def upsert_offer(engine: Engine, offer: RetailerOffer) -> bool:
    """Persist one offer; return True for a new offer or changed fields."""
    now = _iso(offer.source_fetched_at)
    with engine.begin() as connection:
        retailer = connection.execute(
            select(retailers.c.id).where(
                retailers.c.source_key == offer.source_key,
                retailers.c.market_code == offer.market_code,
            )
        ).scalar_one_or_none()
        if retailer is None:
            retailer = connection.execute(
                retailers.insert().values(
                    source_key=offer.source_key,
                    market_code=offer.market_code,
                    name=offer.source_key.title(),
                    enabled=True,
                )
            ).inserted_primary_key[0]
        id_match = (
            connection.execute(
                select(offers).where(
                    offers.c.retailer_id == retailer,
                    offers.c.source_product_id == offer.source_product_id,
                )
            )
            .mappings()
            .one_or_none()
            if offer.source_product_id
            else None
        )
        url_match = (
            connection.execute(
                select(offers).where(
                    offers.c.retailer_id == retailer,
                    offers.c.canonical_url == offer.canonical_url,
                )
            )
            .mappings()
            .one_or_none()
        )
        if id_match and url_match and id_match["id"] != url_match["id"]:
            raise ValueError("source ID and canonical URL identify different existing offers")
        existing = id_match or url_match
        if (
            existing
            and existing["source_product_id"]
            and offer.source_product_id
            and existing["source_product_id"] != offer.source_product_id
        ):
            raise ValueError("canonical URL already belongs to a different source product ID")
        values = {
            "source_product_id": offer.source_product_id
            or (existing["source_product_id"] if existing else None),
            "source_url": offer.source_url,
            "canonical_url": offer.canonical_url,
            "title": offer.title,
            "brand": offer.brand,
            "category": offer.category,
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
            values["price_amount"] = existing["price_amount"]
            values["currency"] = existing["currency"]
            values["original_price_amount"] = existing["original_price_amount"]
        if existing:

            def differs(key: str, value: Any) -> bool:
                old = existing[key]
                if (
                    key in {"price_amount", "original_price_amount"}
                    and old is not None
                    and value is not None
                ):
                    return Decimal(old) != Decimal(value)
                return old != value

            changed = any(
                differs(key, val)
                for key, val in values.items()
                if key not in {"updated_at", "last_seen_at", "source_fetched_at"}
            )
            connection.execute(
                offers.update().where(offers.c.id == existing["id"]).values(**values)
            )
            offer_id = existing["id"]
        else:
            changed = True
            offer_id = connection.execute(
                offers.insert().values(retailer_id=retailer, created_at=now, **values)
            ).inserted_primary_key[0]
        if offer.price_amount is not None and offer.currency is not None:
            previous = connection.execute(
                select(observations.c.price_amount, observations.c.currency)
                .where(observations.c.offer_id == offer_id)
                .order_by(observations.c.id.desc())
                .limit(1)
            ).first()
            pair = (_money_text(offer.price_amount), offer.currency)
            if (
                previous is None
                or Decimal(previous[0]) != offer.price_amount
                or previous[1] != offer.currency
            ):
                connection.execute(
                    observations.insert().values(
                        offer_id=offer_id,
                        price_amount=pair[0],
                        currency=pair[1],
                        observed_at=now,
                        source_fetched_at=now,
                    )
                )
        return changed


def save_run(engine: Engine, report: dict[str, Any]) -> None:
    values = dict(report)
    values["error_summary"] = json.dumps(values["error_summary"], ensure_ascii=False)
    values["completeness"] = json.dumps(values["completeness"], ensure_ascii=False)
    with engine.begin() as connection:
        connection.execute(runs.insert().values(**values))


def load_run(engine: Engine, run_id: str) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = (
            connection.execute(select(runs).where(runs.c.run_id == run_id)).mappings().one_or_none()
        )
    if row is None:
        return None
    data = dict(row)
    data["error_summary"] = json.loads(data["error_summary"])
    data["completeness"] = json.loads(data["completeness"])
    return data


def export_catalogue(engine: Engine, output: Path, source_key: str | None = None) -> int:
    query = select(offers, retailers.c.source_key, retailers.c.market_code).join(
        retailers, offers.c.retailer_id == retailers.c.id
    )
    if source_key:
        query = query.where(retailers.c.source_key == source_key)
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(query).mappings()]
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "source_key",
        "market_code",
        "source_product_id",
        "source_url",
        "title",
        "brand",
        "category",
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

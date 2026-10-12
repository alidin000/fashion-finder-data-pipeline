"""Adopt a pre-Django SQLite catalogue without losing its product history."""

import sqlite3
from datetime import UTC, datetime

from django.db import connections

from fashion_finder_data.catalogue.models import CollectionRun, PriceObservation, RetailerOffer
from fashion_finder_data.schema import RetailerOffer as ParsedOffer
from fashion_finder_data.storage import open_database, upsert_offer


def test_legacy_sqlite_adoption_preserves_rows_and_supports_new_writes(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE retailer (
                id INTEGER PRIMARY KEY, source_key VARCHAR NOT NULL,
                market_code VARCHAR NOT NULL, name VARCHAR NOT NULL, enabled BOOLEAN NOT NULL,
                UNIQUE(source_key, market_code)
            );
            CREATE TABLE retailer_offer (
                id INTEGER PRIMARY KEY, retailer_id INTEGER NOT NULL REFERENCES retailer(id),
                source_product_id VARCHAR, source_url TEXT NOT NULL, canonical_url TEXT NOT NULL,
                title TEXT NOT NULL, brand VARCHAR, category VARCHAR, description TEXT,
                price_amount VARCHAR, currency VARCHAR, original_price_amount VARCHAR,
                image_urls TEXT NOT NULL, colour_names TEXT NOT NULL, size_options TEXT NOT NULL,
                availability_status VARCHAR NOT NULL, source_fetched_at VARCHAR NOT NULL,
                created_at VARCHAR NOT NULL, updated_at VARCHAR NOT NULL,
                last_seen_at VARCHAR NOT NULL, status VARCHAR NOT NULL,
                UNIQUE(retailer_id, source_product_id), UNIQUE(retailer_id, canonical_url)
            );
            CREATE TABLE price_observation (
                id INTEGER PRIMARY KEY, offer_id INTEGER NOT NULL REFERENCES retailer_offer(id),
                price_amount VARCHAR NOT NULL, currency VARCHAR NOT NULL,
                observed_at VARCHAR NOT NULL, source_fetched_at VARCHAR NOT NULL
            );
            CREATE TABLE collection_run (
                run_id VARCHAR PRIMARY KEY, source_key VARCHAR NOT NULL,
                market_code VARCHAR NOT NULL, started_at VARCHAR NOT NULL,
                finished_at VARCHAR NOT NULL, status VARCHAR NOT NULL,
                records_seen INTEGER NOT NULL, records_valid INTEGER NOT NULL,
                records_rejected INTEGER NOT NULL, records_upserted INTEGER NOT NULL,
                records_deduplicated INTEGER NOT NULL, error_summary TEXT NOT NULL,
                completeness TEXT NOT NULL, adapter_version VARCHAR NOT NULL
            );
            INSERT INTO retailer VALUES (1, 'reserved', 'HU', 'Reserved', 1);
            INSERT INTO retailer_offer VALUES (
                1, 1, 'SKU-1', 'https://www.reserved.com/hu/hu/coat',
                'https://www.reserved.com/hu/hu/coat', 'Legacy coat', NULL, 'Kabátok', NULL,
                '25995', 'HUF', NULL, '[]', '[]', '[]', 'unknown',
                '2026-10-12T10:00:00+00:00', '2026-10-12T10:00:00+00:00',
                '2026-10-12T10:00:00+00:00', '2026-10-12T10:00:00+00:00', 'active'
            );
            INSERT INTO price_observation VALUES (
                1, 1, '25995', 'HUF', '2026-10-12T10:00:00+00:00',
                '2026-10-12T10:00:00+00:00'
            );
            INSERT INTO collection_run VALUES (
                'run-1', 'reserved', 'HU', '2026-10-12T10:00:00+00:00',
                '2026-10-12T10:00:01+00:00', 'success', 1, 1, 0, 1, 0, '[]', '{}', 'v1'
            );
        """)
    database = open_database(path)
    row = RetailerOffer.objects.using(database.alias).get(pk=1)
    assert (row.title, row.category, row.source_category) == ("Legacy coat", "coats", "Kabátok")
    assert PriceObservation.objects.using(database.alias).count() == 1
    assert CollectionRun.objects.using(database.alias).get(pk="run-1").records_parsed == 0
    assert connections[database.alias].introspection.table_names().count("retailer_offer") == 1
    upsert_offer(
        database,
        ParsedOffer(
            source_key="reserved",
            source_url="https://www.reserved.com/hu/hu/new-coat",
            canonical_url="https://www.reserved.com/hu/hu/new-coat",
            source_product_id="SKU-2",
            title="New coat",
            source_fetched_at=datetime(2026, 10, 12, tzinfo=UTC),
        ),
    )
    assert RetailerOffer.objects.using(database.alias).count() == 2

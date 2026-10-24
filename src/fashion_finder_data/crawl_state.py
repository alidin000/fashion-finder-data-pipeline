"""Local crawler state: HTTP validators, resumable discovery runs and policy caches.

It lives in its own SQLite file (``data/crawl-state.sqlite``), separate from the
catalogue. It is plain ``sqlite3`` because Scrapy callbacks run inside an asyncio
loop, where the Django ORM refuses synchronous queries. Everything here is a cache:
losing it (an ephemeral cron container, API mode) only costs conditional requests
and the ability to resume an interrupted discovery run. Refresh scheduling state
lives in the catalogue database (``scheduler``).

Tables:
- ``runs``: one row per source run, its mode, status and final report.
- ``frontier``: URLs a run scheduled and whether each was processed, so an
  interrupted run resumes where it stopped.
- ``spool``: raw records a run extracted, written as each page is parsed; ingestion
  replays them, and upserts are idempotent, so a resumed run never duplicates offers.
- ``validators``: ETag, Last-Modified and body hash per URL for conditional requests.
- ``host_policy``: cached TDMRep files per host.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .schema import RawProductRecord, canonical_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    report TEXT
);
CREATE INDEX IF NOT EXISTS runs_source ON runs (source_key, started_at);
CREATE TABLE IF NOT EXISTS frontier (
    run_id TEXT NOT NULL,
    url TEXT NOT NULL,
    source_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    detail TEXT,
    meta TEXT,
    PRIMARY KEY (run_id, url)
);
CREATE TABLE IF NOT EXISTS spool (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS validators (
    url TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    sha256 TEXT,
    status INTEGER,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_policy (
    host TEXT PRIMARY KEY,
    tdmrep TEXT,
    fetched_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def product_key(url: str) -> str:
    try:
        return canonical_url(url)
    except ValueError:
        return url


class CrawlStore:
    """A small SQLite store; safe to open from the CLI and from Scrapy callbacks."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ runs
    def begin_run(self, source_key: str, mode: str, run_id: str | None = None) -> str:
        run_id = run_id or uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO runs (run_id, source_key, mode, status, started_at)"
                " VALUES (?, ?, ?, 'running', ?)",
                (run_id, source_key, mode, now_iso()),
            )
        return run_id

    def unfinished_run(self, source_key: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM runs WHERE source_key=? AND status='running'"
            " ORDER BY started_at DESC LIMIT 1",
            (source_key,),
        ).fetchone()
        return dict(row) if row else None

    def finish_run(self, run_id: str, status: str, report: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET status=?, finished_at=?, report=? WHERE run_id=?",
                (status, now_iso(), json.dumps(report, ensure_ascii=False, default=str), run_id),
            )
            self.db.execute("DELETE FROM spool WHERE run_id=?", (run_id,))
            self.db.execute("DELETE FROM frontier WHERE run_id=?", (run_id,))

    def abandon_run(self, run_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET status='abandoned', finished_at=? WHERE run_id=?",
                (now_iso(), run_id),
            )
            self.db.execute("DELETE FROM spool WHERE run_id=?", (run_id,))
            self.db.execute("DELETE FROM frontier WHERE run_id=?", (run_id,))

    def runs(self, source_key: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        query = "SELECT * FROM runs"
        params: list[Any] = []
        if source_key:
            query += " WHERE source_key=?"
            params.append(source_key)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = []
        for row in self.db.execute(query, params):
            item = dict(row)
            item["report"] = json.loads(item["report"]) if item["report"] else None
            rows.append(item)
        return rows

    # ------------------------------------------------------------------ frontier
    def enqueue(
        self,
        run_id: str,
        source_key: str,
        url: str,
        kind: str,
        priority: int = 0,
        meta: dict[str, Any] | None = None,
    ) -> bool:
        """Record a scheduled URL; False when this run already has it."""
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO frontier (run_id, url, source_key, kind, priority, meta)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, url, source_key, kind, priority, json.dumps(meta or {})),
            )
        return cursor.rowcount == 1

    def mark(self, run_id: str, url: str, status: str, detail: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE frontier SET status=?, detail=? WHERE run_id=? AND url=?",
                (status, detail, run_id, url),
            )

    def frontier(self, run_id: str, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM frontier WHERE run_id=?"
        params: list[Any] = [run_id]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY priority DESC, rowid"
        rows = []
        for row in self.db.execute(query, params):
            item = dict(row)
            item["meta"] = json.loads(item["meta"] or "{}")
            rows.append(item)
        return rows

    # ------------------------------------------------------------------ spool
    def spool(self, run_id: str, records: Iterable[RawProductRecord]) -> None:
        records = list(records)
        if not records:
            return
        with self.db:
            start = self.db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM spool WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            self.db.executemany(
                "INSERT INTO spool (run_id, seq, record) VALUES (?, ?, ?)",
                [
                    (run_id, start + index, record.model_dump_json())
                    for index, record in enumerate(records, 1)
                ],
            )

    def spooled(self, run_id: str) -> list[RawProductRecord]:
        return [
            RawProductRecord.model_validate_json(row[0])
            for row in self.db.execute(
                "SELECT record FROM spool WHERE run_id=? ORDER BY seq", (run_id,)
            )
        ]

    # ------------------------------------------------------------------ validators
    def validators(self, url: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM validators WHERE url=?", (url,)).fetchone()
        return dict(row) if row else None

    def save_validators(
        self,
        url: str,
        source_key: str,
        *,
        etag: str | None,
        last_modified: str | None,
        body: bytes | None,
        status: int,
    ) -> None:
        digest = hashlib.sha256(body).hexdigest() if body is not None else None
        with self.db:
            self.db.execute(
                "INSERT INTO validators (url, source_key, etag, last_modified, sha256, status,"
                " fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET"
                " etag=excluded.etag, last_modified=excluded.last_modified,"
                " sha256=COALESCE(excluded.sha256, validators.sha256),"
                " status=excluded.status, fetched_at=excluded.fetched_at",
                (url, source_key, etag, last_modified, digest, status, now_iso()),
            )

    # ------------------------------------------------------------------ host policy
    def host_policy(self, host: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM host_policy WHERE host=?", (host,)).fetchone()
        return dict(row) if row else None

    def save_host_policy(self, host: str, tdmrep: Any) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO host_policy (host, tdmrep, fetched_at) VALUES (?, ?, ?)"
                " ON CONFLICT(host) DO UPDATE SET tdmrep=excluded.tdmrep,"
                " fetched_at=excluded.fetched_at",
                (host, json.dumps(tdmrep), now_iso()),
            )

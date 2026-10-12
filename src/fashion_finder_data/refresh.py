"""Scheduled local imports for new, authorized, timestamped feed files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from .collectors import SOURCES
from .feed import import_feed
from .source_policy import load_policies
from .storage import Database, open_database

MAX_FEED_BYTES = 20 * 1024 * 1024


def _load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or any(
        key not in SOURCES or not isinstance(value, str) for key, value in state.items()
    ):
        raise ValueError("invalid refresh state file")
    return state


def _save_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def refresh_once(
    database: Database,
    data_dir: Path,
    feed_dir: Path,
    policy_path: Path | None,
    state_path: Path,
) -> dict:
    """Import changed <source>.csv files once, never refreshing unchanged observations."""
    policies = load_policies(policy_path)
    state = _load_state(state_path)
    results = []
    for source in SOURCES:
        path = feed_dir / f"{source}.csv"
        if not path.is_file():
            continue
        policy = policies.get((source, "HU"))
        if (
            policy is None
            or policy.approval_status != "approved"
            or policy.access_method != "feed-csv"
        ):
            results.append({"source_key": source, "status": "blocked_by_policy"})
            continue
        if path.stat().st_size > MAX_FEED_BYTES:
            results.append({"source_key": source, "status": "feed_too_large"})
            continue
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if state.get(source) == digest:
            results.append({"source_key": source, "status": "unchanged"})
            continue
        try:
            run = import_feed(
                database,
                data_dir,
                path,
                source,
                require_observed_at=True,
            )
        except (OSError, ValueError) as exc:
            results.append({"source_key": source, "status": "failed", "detail": str(exc)})
            continue
        if run["status"] == "success":
            state[source] = digest
            _save_state(state_path, state)
        results.append(
            {
                "source_key": source,
                "status": run["status"],
                "run_id": run["run_id"],
                "records_valid": run["records_valid"],
                "records_rejected": run["records_rejected"],
            }
        )
    return {
        "status": "attention"
        if any(item["status"] not in {"success", "unchanged"} for item in results)
        else "idle"
        if not results or all(item["status"] == "unchanged" for item in results)
        else "success",
        "sources": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, default=Path(os.getenv("FASHION_FINDER_DATA_DIR", "data"))
    )
    parser.add_argument("--feed-dir", type=Path, default=None)
    parser.add_argument("--state-file", type=Path, default=None)
    policy = os.getenv("FASHION_FINDER_SOURCE_POLICIES")
    parser.add_argument("--policy", type=Path, default=Path(policy) if policy else None)
    parser.add_argument("--interval-seconds", type=int, default=3600)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 60 <= args.interval_seconds <= 86400:
        parser.error("--interval-seconds must be between 60 and 86400")
    feed_dir = args.feed_dir or args.data_dir / "feeds"
    state_path = args.state_file or args.data_dir / "worker-state.json"
    database = open_database(args.data_dir / "fashion_finder.sqlite")
    from .analytics import prune_metrics

    while True:
        try:
            result = refresh_once(database, args.data_dir, feed_dir, args.policy, state_path)
            result["analytics_rows_pruned"] = prune_metrics(database)
        except (OSError, TypeError, ValueError) as exc:
            result = {"status": "attention", "detail": str(exc), "sources": []}
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.once:
            return 0 if result["status"] != "attention" else 1
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

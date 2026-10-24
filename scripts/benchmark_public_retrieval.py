"""Measure photo and look search on the public-dataset practice catalogue.

Build the practice folder first (``python -m fashion_finder_data.practice.build``).
Tasks, all held out from the gallery they search:
  a            photo → same product (ABO; one photo per product is the query)
  b            photo → same category (ABO held-out photos, held-out GLAMI products)
  c            look: English ABO product name → that product
  d_name       look: Hungarian GLAMI product name → that product
  d_phrase     look: Hungarian phrase such as "fekete női kabát" → products of that
               category, colour and gender (colour labels are partial: a lower bound)
  d_phrase_en  the same phrases in English, to show what translation would gain

Modes:
  embedding  rank by raw embeddings (Script B); any pinned encoder, cached per version,
             at most --max-new-encodings photos encoded per run (rerun to continue)
  real       load the gallery into a temporary data directory and call the shipped
             search_indexed_image / search_indexed_text (Harness A); --photo-options and
             --look-options are passed to them as keyword arguments

Example:
  .venv/bin/python scripts/benchmark_public_retrieval.py --mode real \\
    --practice-dir "$PRACTICE" --work-dir "$WORK" \\
    --model-path "$MODELS/fashion-siglip-vision-int8.onnx" \\
    --text-model-path "$MODELS/fashion-siglip-text-int8.onnx" \\
    --tokenizer-path "$MODELS/fashion-siglip-tokenizer.json" \\
    --look-options '{"blend": false}' --output results.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from fashion_finder_data.image_index import search_indexed_image, search_indexed_text
from fashion_finder_data.practice import CATALOGUE_FILE
from fashion_finder_data.practice.benchmark import (
    LOOK_RESERVED,
    MAX_NEW_ENCODINGS,
    PHOTO_RESERVED,
    PHOTO_TASKS,
    TASKS,
    build_split,
    checked_options,
    peak_rss_mb,
    prepare_real_data_dir,
    run_embedding,
    run_real,
)
from fashion_finder_data.practice.loader import read_catalogue

COLUMNS = (
    ("queries", "queries"),
    ("hit_at_1", "Hit@1"),
    ("hit_at_5", "Hit@5"),
    ("mrr_at_10", "MRR@10"),
    ("ndcg_at_10", "nDCG@10"),
    ("recall_at_10", "R@10"),
    ("precision_at_10", "P@10"),
    ("latency_ms_p50", "p50 ms"),
    ("latency_ms_p95", "p95 ms"),
)


def table(tasks: dict) -> str:
    lines = [
        "| task | " + " | ".join(label for _, label in COLUMNS) + " |",
        "|---" * (len(COLUMNS) + 1) + "|",
    ]
    for task, values in tasks.items():
        cells = [values.get(key) for key, _ in COLUMNS]
        lines.append(
            f"| {task} | " + " | ".join("" if cell is None else str(cell) for cell in cells) + " |"
        )
    return "\n".join(lines)


def _file_label(path: Path | None) -> str | None:
    return f"{path.name} ({path.stat().st_size} bytes)" if path else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[2:]),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--practice-dir", type=Path, required=True)
    parser.add_argument(
        "--work-dir", type=Path, required=True, help="Embedding cache and temporary data dirs"
    )
    parser.add_argument("--mode", choices=["embedding", "real"], default="embedding")
    parser.add_argument("--tasks", default=",".join(TASKS), help="Comma-separated task names")
    parser.add_argument(
        "--model-path", type=Path, help="Pinned vision model; default colour layout"
    )
    parser.add_argument("--text-model-path", type=Path)
    parser.add_argument("--tokenizer-path", type=Path)
    parser.add_argument("--photo-options", help="JSON keyword arguments for search_indexed_image")
    parser.add_argument("--look-options", help="JSON keyword arguments for search_indexed_text")
    parser.add_argument("--max-new-encodings", type=int, default=MAX_NEW_ENCODINGS)
    parser.add_argument("--output", type=Path, help="Write the JSON report here")
    args = parser.parse_args(argv)

    tasks = [task.strip() for task in args.tasks.split(",") if task.strip()]
    unknown = sorted(set(tasks) - set(TASKS))
    if unknown:
        parser.error(f"unknown tasks: {', '.join(unknown)}; choose from {', '.join(TASKS)}")
    skipped = {}
    if not (args.text_model_path and args.tokenizer_path):
        skipped = {task: "no text model" for task in tasks if task not in PHOTO_TASKS}
        tasks = [task for task in tasks if task in PHOTO_TASKS]
    if not tasks:
        parser.error("no task left to run; look tasks need --text-model-path and --tokenizer-path")
    if not 1 <= args.max_new_encodings <= MAX_NEW_ENCODINGS:
        parser.error(f"--max-new-encodings must be between 1 and {MAX_NEW_ENCODINGS}")
    try:
        if args.mode == "embedding" and (args.photo_options or args.look_options):
            raise ValueError("--photo-options and --look-options apply to --mode real")
        photo_options = checked_options(search_indexed_image, args.photo_options, PHOTO_RESERVED)
        look_options = checked_options(search_indexed_text, args.look_options, LOOK_RESERVED)
        practice_dir = args.practice_dir.resolve()
        split = build_split(read_catalogue(practice_dir))
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    report = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "command": " ".join([Path(sys.argv[0]).name, *(argv or sys.argv[1:])]),
        "mode": args.mode,
        "models": {
            "vision": _file_label(args.model_path) or "colour-layout (built in)",
            "text": _file_label(args.text_model_path),
        },
        "photo_options": photo_options,
        "look_options": look_options,
        "dataset": split.sizes(),
        "skipped_tasks": skipped,
    }
    models = {
        "model_path": args.model_path,
        "text_model_path": args.text_model_path,
        "tokenizer_path": args.tokenizer_path,
    }
    if args.mode == "embedding":
        result = run_embedding(
            split,
            practice_dir,
            args.work_dir / "embeddings",
            tasks,
            max_new_encodings=args.max_new_encodings,
            **models,
        )
    else:
        catalogue = (practice_dir / CATALOGUE_FILE).read_bytes()
        key = hashlib.sha256(catalogue + str(_file_label(args.model_path)).encode()).hexdigest()[
            :16
        ]
        data_dir = args.work_dir / "real" / key
        report["real_data_dir"] = str(data_dir)
        report["prepare"] = prepare_real_data_dir(split, practice_dir, data_dir, args.model_path)
        result = run_real(
            split,
            practice_dir,
            data_dir,
            tasks,
            photo_options=photo_options,
            look_options=look_options,
            **models,
        )
    report.update(result)
    report["peak_rss_mb"] = peak_rss_mb()
    body = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
    print(body if not report["tasks"] else table(report["tasks"]))
    print(f"status: {report['status']}; peak RSS {report['peak_rss_mb']} MB", file=sys.stderr)
    return 0 if report["status"] == "complete" else 3


if __name__ == "__main__":
    raise SystemExit(main())

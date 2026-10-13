# Consented shopper-task evaluation

Use 30 real, consented tasks in one launch category. Keep raw query text,
photos, participant names, and consent records in a private research store.
The local evaluation file contains only pseudonymous task IDs, pointers to
that private evidence, offer IDs, human usefulness labels, and latency. Put it
under ignored `data/evaluation/`.

For each task, run the text flow and record the ordered first-page offer IDs.
Mark the IDs of options the participant considers useful and current. If photo
matching is available under source policy, record its ordered top five and
the IDs the participant found relevant. Use the **same task and catalogue**
for text and photo. Measure elapsed milliseconds from submit to results.

The following is one **synthetic format example**, not participant evidence:

```json
[
  {
    "task_id": "task-001",
    "consent_ref": "private/consent/001",
    "input_ref": "private/task-input/001",
    "category": "jackets",
    "text_result_ids": [12, 43, 55],
    "text_useful_ids": [12, 43, 55],
    "text_latency_ms": 450,
    "photo_result_ids": [55, 12, 43],
    "photo_relevant_ids": [55, 12],
    "photo_latency_ms": 900
  }
]
```

Omit all three `photo_*` fields when no permitted photo comparison was run.
The evaluator rejects unknown fields, so raw queries or photos cannot be
accidentally added. The `consent_ref` and `input_ref` are references only; the
tool does not inspect them. Do not put personal data in the references.

```bash
.venv/bin/fashion-finder evaluate-tasks data/evaluation/jackets.json \
  --category jackets --policy data/source-policies.json \
  --output data/reports/jackets-tasks.json
```

The text gate needs **exactly 30 tasks**, with at least **24** yielding three
currently eligible useful offers from **at least two approved shops per task**.
This is a conservative reading of “across the two shops” in the implementation
plan. The report lists failed task IDs and current coverage counts. It reports
photo top-five relevance and latency on tasks with permitted image results,
beside text top-five relevance on those same tasks. There is no automatic photo
release threshold; review the measured tradeoff before enabling photo search.

Usefulness, consent, and retailer permission are human assertions. The tool
checks offer freshness and declared policy, but cannot verify those assertions
or create the 30 real tasks.

## Replay the ranking code

`evaluate-tasks` above scores manually recorded result IDs. For reproducible
ranking evidence, use `evaluate-rankings` with independent relevance labels.
This command actually runs the current text search and, when a local permitted
image index exists, the current photo search. It applies the same category,
colour, size, and HUF budget to both. It measures local processing time; browser
and network latency require a separate deployed smoke test.

Keep this input under ignored `data/evaluation/`. It contains private query
text and a relative path to a consented photo. The output includes result IDs
and aggregate measures, but never echoes the query, photo path, or consent
reference. Use a copy of the catalogue that the worker is not updating.

```json
[
  {
    "task_id": "task-001",
    "consent_ref": "private/consent/001",
    "input_ref": "private/task-input/001",
    "category": "jackets",
    "query": "brown jacket",
    "colour": "Brown",
    "size": "M",
    "max_price": "15000",
    "photo_path": "photos/task-001.jpg",
    "relevant_offer_ids": [12, 43, 55]
  }
]
```

The offer IDs are human relevance labels, not claimed search results. Omit
`photo_path` for a text-only task. A photo path must stay inside the task-file
directory. The command rejects absent, invalid, oversized, or unpermitted
photos and will not fetch a photo URL. The task file needs 1–30 distinct task
IDs and input references; only **exactly 30** can satisfy the text task gate.

```bash
.venv/bin/fashion-finder --data-dir data evaluate-rankings \
  data/evaluation/jackets-replay.json --category jackets \
  --policy data/source-policies.json \
  --output data/reports/jackets-replay.json
```

The report records the actual ordered text and photo offer IDs for every task,
current relevant-offer coverage, top-five relevance, and measured local
latency. It cannot verify consent, label quality, publisher rights, or real
shopper usefulness. Review those inputs before using the report for a release
decision.

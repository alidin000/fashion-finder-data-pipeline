# Tests

Run `pytest` from the repository root. `test_pipeline.py` uses synthetic HTML and records to cover parsing, validation, repeat-run upserts, price changes, missing prices and isolated source failures.

Live source checks are separate from unit tests and must use a small `--limit`.

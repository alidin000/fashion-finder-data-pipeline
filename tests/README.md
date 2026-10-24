# Tests

Run `pytest` from the repository root. `test_pipeline.py` uses synthetic HTML and records to cover parsing, validation, repeat-run upserts, price changes, missing prices and isolated source failures.

Live source checks are separate from unit tests and must use a small `--limit`.

Scraper tests use recorded-style fixtures only: `tests/fixtures/pages/` (synthetic shop
pages and sitemaps modelled on the structures the research recorded) and
`tests/fixtures/replay/` (responses served by `fashion_finder_data.replay` in place of the
network, so the real crawler, politeness middlewares and ingest run offline).
`test_politeness.py` runs the Scrapy middlewares against a local fake shop in a
subprocess. No test contacts a retailer.

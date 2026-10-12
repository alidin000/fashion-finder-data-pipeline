# Fashion Finder — Data Pipeline

A local-first proof of concept for collecting, normalizing, and comparing fashion-product listings from multiple sources.

## Goal

Build a **local product-ingestion pipeline** for Fashion Finder. It should collect small test batches, preserve raw records, normalize them to a common product-offer schema, track price observations, and export a catalogue for the search frontend.

This repository begins as a plan and engineering scaffold. It does **not** yet contain working retailer collectors or a populated product catalogue.

## Initial source strategy

- **General-purpose tooling:** Scrapy, Playwright for Python, and Crawlee for Python.
- **Retailer adapters to evaluate:** Zara, H&M, and Reserved, using explicitly licensed community code only where useful.
- **Pull&Bear:** build an independent adapter with the general-purpose tools; do not copy code from a surfaced repository that has no explicit licence.
- **Optional experiment:** Google Shopping results through a community scraper. Treat results as search candidates, not a complete canonical retailer catalogue.

See [source and licence review](docs/source-review.md) for the repositories and point-in-time findings.

## Pipeline

1. Collect a small batch from one source.
2. Save raw source output to `data/raw/` as JSONL.
3. Normalize to the shared schema.
4. Validate fields and flag missing or malformed data.
5. Deduplicate by source plus source product ID/URL.
6. Upsert offers into local SQLite and append price observations.
7. Export normalized results and a collection-run report.

See [architecture](docs/architecture.md), [data schema](docs/data-schema.md), and [roadmap](docs/roadmap.md).

## Initial stack

- Python 3.12
- Scrapy
- Playwright for Python (only when browser rendering is needed)
- Pydantic for schema validation
- SQLAlchemy + SQLite for local persistence
- JSONL for raw snapshots and portable exports
- Pytest for tests

Crawlee is a comparison candidate, not another required runtime component. Benchmark it against Scrapy before deciding to adopt it.

## Windows quick start

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
python -m playwright install chromium
```

At this stage, the CLI is a placeholder. No retailer data is collected until an adapter is implemented and explicitly invoked.

## First success criteria

- Source inventory and code-licence decisions are documented.
- Fixture records can pass through normalization and validation.
- A small collector batch produces raw JSONL and normalized output.
- Repeat runs update existing offers rather than creating duplicates.
- Each run records item counts, failures and timestamps.
- Prices, currency and product URLs can be manually verified for a sample.

## Important boundaries

- Software licences cover source code, not retailer content collected through it.
- Public visibility does not itself grant permission to reuse source code.
- Site terms, content rights and source-specific limitations are separate from code licensing.
- Do not design collectors to bypass logins, CAPTCHAs, access controls, or explicit blocks.
- This is a local feasibility experiment; country coverage, freshness and output quality remain unverified until tested.

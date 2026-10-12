# Implementation Roadmap

## Phase 0 — Repository and local environment
- [x] Document initial architecture and candidate sources.
- [x] Record point-in-time code licence findings.
- [ ] Create Python package and local development environment.
- [ ] Add .env.example only if configuration secrets become necessary; never commit real tokens.

## Phase 1 — Data contract and fixtures
- [ ] Define RawProductRecord, RetailerOffer and CollectionRun schemas.
- [ ] Add small, non-sensitive fixture records.
- [ ] Normalize price, currency, category, colour and size fields.
- [ ] Test missing prices, decimal/comma formats, malformed URLs and unknown availability.
- [ ] Implement JSONL input/output helpers.

## Phase 2 — Local persistence
- [ ] Create SQLite tables for retailers, offers, price observations and collection runs.
- [ ] Implement idempotent upsert keyed by retailer + source product ID; fall back to canonical URL.
- [ ] Insert a price observation when the observed price/currency changes.
- [ ] Add JSONL and CSV exports.
- [ ] Generate a run report with field completeness.

## Phase 3 — First retailer adapter
- [ ] Inspect and test the Zara community code in isolation.
- [ ] Save a small raw-response fixture.
- [ ] Update obsolete dependencies or port extraction logic to Scrapy.
- [ ] Run a small bounded sample and manually verify at least 10 results.
- [ ] Record unsupported fields rather than guessing.

## Phase 4 — H&M and Reserved
- [ ] Adapt H&M parsing with an explicit source-market setting.
- [ ] Inspect Reserved repository source files and validate current page structure.
- [ ] Make each collector independently runnable.
- [ ] Ensure one adapter failure does not terminate the whole pipeline.

## Phase 5 — Pull&Bear and optional source
- [ ] Implement a clean-room Pull&Bear adapter; do not copy the unlicensed repository.
- [ ] Evaluate Google Shopping scraping only as an optional discovery source.
- [ ] Benchmark Crawlee against Scrapy on maintainability, extraction success and runtime.

## Phase 6 — Quality and refresh
- [ ] Add bounded retries and timeouts.
- [ ] Store fetch timestamps, source URL and adapter version.
- [ ] Detect stale, removed or malformed listings.
- [ ] Export quality metrics per retailer/category.
- [ ] Keep a manual verification sample.

## Phase 7 — Search-ready catalogue
- [ ] Define a category taxonomy shared by all sources.
- [ ] Add text query, budget filter and price ascending/descending over normalized data.
- [ ] Evaluate image embeddings only when catalogue coverage is adequate.

## Definition of done for first proof of concept
- At least one adapter works end-to-end locally.
- Raw records are preserved.
- Normalized records pass tests and are stored idempotently.
- Repeat runs update offers and record price changes.
- A run report explains source failures and data completeness.
- Results are manually spot-checked against source pages.

# Implementation Roadmap

## Phase 0 — Repository and local environment
- [x] Document initial architecture and candidate sources.
- [x] Record point-in-time code licence findings.
- [x] Create Python package and local development environment.
- [ ] Add .env.example only if configuration secrets become necessary; never commit real tokens.

## Phase 1 — Data contract and fixtures
- [x] Define RawProductRecord, RetailerOffer and CollectionRun schemas.
- [x] Add small, non-sensitive synthetic test records.
- [x] Normalize price, currency, category, colour and size fields when supplied.
- [x] Test missing prices, decimal/comma formats, malformed URLs and unknown availability.
- [x] Implement JSONL input/output helpers.

## Phase 2 — Local persistence
- [x] Create SQLite tables for retailers, offers, price observations and collection runs.
- [x] Implement idempotent upsert keyed by retailer + source product ID; fall back to canonical URL.
- [x] Insert a price observation when the observed price/currency changes.
- [x] Add JSONL and CSV exports.
- [x] Generate a run report with field completeness.

## Phase 3 — First retailer adapter
- [x] Implement and live-check a bounded Reserved Hungary adapter across coats and dresses.
- [x] Preserve a raw JSONL record, store one offer, and verify its price and URL.
- [x] Cross-check at least 10 results across categories against live product-page metadata.
- [x] Record unsupported fields as unknown rather than guessing.

## Phase 4 — Zara, H&M and Pull&Bear
- [ ] Obtain allowed access or feeds for the three currently blocked/unparseable storefronts.
- [x] Add a local CSV importer for retailer-provided authorized product feeds.
- [ ] Verify source-specific extraction on small Hungarian samples.
- [x] Make each configured collector independently runnable.
- [x] Ensure one adapter failure does not terminate the whole pipeline.

## Phase 5 — Additional sources and coverage
- [ ] Finish a clean-room Pull&Bear adapter when a usable, allowed data source is available; do not copy the unlicensed repository.
- [ ] Evaluate Google Shopping scraping only as an optional discovery source.
- [ ] Benchmark Crawlee against Scrapy on maintainability, extraction success and runtime.

## Phase 6 — Quality and refresh
- [x] Add bounded retries and timeouts for web collection.
- [x] Store fetch timestamps, source URL and adapter version.
- [x] Report stale observations and malformed stored values without changing stock state.
- [ ] Establish a source-specific removal policy before marking listings unavailable.
- [x] Export quality metrics per retailer/category.
- [x] Keep the ten-offer Reserved verification sample in the source-status document.

## Phase 7 — Search-ready catalogue
- [x] Define shared keys for current categories and preserve unmapped source labels.
- [x] Add text query, budget filter and price ascending/descending over normalized data.
- [ ] Evaluate image embeddings only when catalogue coverage is adequate.

## Definition of done for first proof of concept
- At least one adapter works end-to-end locally.
- Raw records are preserved.
- Normalized records pass tests and are stored idempotently.
- Repeat runs update offers and record price changes.
- A run report explains source failures and data completeness.
- Results are manually spot-checked against source pages.

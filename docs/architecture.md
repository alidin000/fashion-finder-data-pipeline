# Architecture

## Objective

Keep source-specific extraction separate from normalization and persistence. This makes a collector replaceable when its page structure changes or a better feed becomes available.

```text
Retailer / feed / search-results source
                |
                v
       Source adapter (one per source)
                |
                v
        Raw JSONL snapshot archive
                |
                v
       Normalize + validate (Pydantic)
                |
                v
        Deduplicate / upsert records
                |
                v
   SQLite offers + price observations + run log
                |
                v
       JSONL / CSV export for search
```

## Responsibilities

### 1. Source adapter
- Accept bounded input such as market, category, start URL and maximum item count.
- Retrieve source records.
- Return source-native fields without silently inventing missing values.
- Record source URL, observed time and adapter version.
- Use finite timeouts and conservative request pacing.

### 2. Raw archive
- Store each run under `data/raw/<source>/<run-id>.jsonl`.
- Preserve the original extracted record plus collection metadata.
- Avoid storing credentials, session cookies or unnecessary personal data.
- Keep fixtures small; raw live catalogues belong in locally ignored data folders.

### 3. Normalization
- Convert fields into a common schema.
- Normalize numeric prices and ISO currency codes.
- Map known category labels to shared keys and preserve the original label.
- Preserve source-native product IDs.
- Represent unknown availability as `unknown`, not `in_stock`.
- Preserve original product and image URLs as source fields; the URL itself does not grant image reuse rights.

### 4. Persistence
Use SQLite initially. Logical tables:
- `retailer`
- `retailer_offer`
- `price_observation`
- `collection_run`

A listing is an offer, not necessarily a globally unique garment. Do not merge similar listings across retailers until there is a separate matching strategy.

### 5. Reporting
Each run should produce:
- source name and market
- start/completion timestamps
- records discovered, parsed, valid, rejected and deduplicated
- errors grouped by type
- completeness rate for price, currency, image URL and product URL
- a small export for manual verification

## Framework decision

- **Scrapy:** default orchestrator for HTTP crawling, extraction and pagination.
- **Playwright:** use selectively for pages that require JavaScript-rendered state.
- **Crawlee for Python:** benchmark as an alternative; do not operate Scrapy and Crawlee as two overlapping schedulers initially.

## Failure handling

One source failing must not stop the entire run. Record the exception and continue other sources. A failed product check should not immediately delete an offer; mark it for recheck or only update availability after a defined repeated-failure policy.

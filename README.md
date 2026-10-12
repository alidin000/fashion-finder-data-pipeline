# Fashion Finder — Data Pipeline

A local-first proof of concept for collecting, normalizing, and comparing fashion-product listings from multiple sources.

## Goal

Build a **local product-ingestion pipeline** for Fashion Finder. It should collect small test batches, preserve raw records, normalize them to a common product-offer schema, track price observations, and export a catalogue for the search frontend.

The first pipeline is implemented. Reserved Hungary passed live runs of ten coats and three dresses on 2026-10-12. Zara and Pull&Bear returned pages without usable product data, and the H&M Scrapy run received HTTP 403. These sources are configured and reported separately, but are **not verified working collectors**. They need an allowed feed or documented access method; do not use access-control bypasses.

## Initial source strategy

- **General-purpose tooling:** Scrapy, Playwright for Python, and Crawlee for Python.
- **Retailer adapters:** Zara, H&M, Reserved, and Pull&Bear. The current collectors use public category/product pages and structured product metadata. No retailer-specific community code is copied.
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
See [live Hungarian source status](docs/live-source-status.md) for the latest bounded check.

## Initial stack

- Python 3.12
- Scrapy (robots-aware HTTP collection)
- Playwright for Python (optional investigation of rendered pages; the current pipeline does not rely on a browser)
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
# Optional: only for browser-rendering investigation
python -m playwright install chromium
```

From the repository root, try one bounded Reserved sample:

```powershell
fashion-finder sources list
fashion-finder collect reserved --limit 1
fashion-finder collect all --limit 1
fashion-finder collect reserved --limit 3 --url https://www.reserved.com/hu/hu/noi/ruhak
fashion-finder report <run-id>
fashion-finder export data/exports/catalogue.csv
```

`collect all` reports each retailer independently and exits nonzero when any source fails. Use `--url https://...` with a single source to test a specific Hungarian category or product URL. Only HTTPS pages on that retailer's Hungarian storefront are accepted. The CLI never logs in, solves CAPTCHAs, or works around HTTP 403.

Reserved recognizes the public `kabatok`, `ruhak`, and `puloverek` category paths as coats, dresses, and sweaters. Other Reserved category overrides retain an unknown category until mapped. The H&M adapter can read complete HUF offers from category cards if the page is accessible, but the live Scrapy check here was blocked before parsing.

Every collection writes raw JSONL under `data/raw/<source>/`, a run report under `data/reports/`, offers and price changes in `data/fashion_finder.sqlite`, and refreshed JSONL/CSV exports under `data/exports/`. These local outputs are ignored by Git. Repeat runs update an offer and add a price observation only when its price or currency changes.

Search the stored HUF offers without making another retailer request:

```powershell
fashion-finder search --query dzseki --max-price 30000 --sort price-asc
fashion-finder search --category dresses --source reserved --sort recent --limit 10
```

Search returns JSONL with source links, prices, category, image URLs, and last-seen time. The default currency is HUF; use `--currency` for another stored currency. Budget comparisons and price sorting use exact decimal values within one currency. Results reflect the last collection, so unknown stock remains `unknown`.

Known English and Hungarian category labels map to shared keys such as `coats`, `jackets`, `dresses`, `sweaters`, and `shirts`. The original label is kept as `source_category`. Unknown labels stay unclassified and appear in quality reports instead of being guessed. Opening an older SQLite database adds `source_category` and maps its existing category values once.

Check freshness and field quality by retailer and category:

```powershell
fashion-finder quality --stale-after-days 7
fashion-finder quality --output data/reports/quality-check.json
```

The report counts old observations, missing fields, unmapped categories, malformed stored values, and unknown availability. It does not contact retailers or change offer status. An old observation means the listing needs another check; it does not prove the item was removed or sold out. `--output` creates a new JSON file and refuses to overwrite an existing one.

To replay a raw file without network access:

```powershell
fashion-finder ingest reserved data/raw/reserved/<run-id>.jsonl
```

If a retailer provides an authorized product export, import a local CSV:

```powershell
fashion-finder import-feed zara path/to/authorized-products.csv
```

The CSV needs `product_url,name,price,currency` headers. Optional headers are
`sku,brand,category,description,original_price,image_url,colour,size,availability`.
Use one product per row, a full HTTPS URL on that retailer's Hungarian storefront,
and `HUF` prices. The command reads a local file; it does not fetch a feed URL or
grant permission to collect retailer content. Valid rows enter the same raw archive,
database, exports, and run report as web collection. Invalid rows are counted and
reported; a partial import exits nonzero. This path is ready for supplied feeds,
but no Zara, H&M, or Pull&Bear feed has been provided or verified.

The first category page only is sampled; pagination and full-catalogue coverage are not implemented. The limit is 1–100 product pages per source, with a 10-second per-domain delay. A JSONL export contains product and image URLs, but this does not grant rights to republish retailer content or images.

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

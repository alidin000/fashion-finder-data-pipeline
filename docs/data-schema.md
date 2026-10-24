# Initial Data Schema

## Retailer
- id — internal primary key
- name — display name
- source_key — stable code such as zara
- market_code — market, e.g. HU
- enabled — whether the adapter is enabled

## RetailerOffer
One record per retailer listing/offer. Do not assume two visually similar records are the same garment.

- id
- retailer_id
- source_product_id — nullable source identifier
- source_url — original product URL
- canonical_url — normalized URL for deduplication where safe
- title
- brand
- category — shared key for recognized categories, otherwise null
- source_category — original source label, including labels outside the current taxonomy
- description — nullable
- price_amount — exact decimal text in SQLite, parsed as Decimal in Python; nullable
- currency — ISO currency code, nullable if not confidently known
- original_price_amount — nullable
- image_urls — JSON array, subject to source usage/storage decisions
- colour_names — JSON array
- size_options — JSON array
- availability_status — in_stock, out_of_stock, unknown
- market_code — inherited from the linked retailer
- source_fetched_at
- created_at, updated_at, last_seen_at
- status — active, needs_recheck, unavailable

Refresh schedule (migration 0004; see [the scraper guide](scraper.md)):
- last_checked_at — when the offer was last checked, even if nothing changed
- next_refresh_at — when it is next due (indexed, also with the retailer)
- refresh_url — the page a refresh reads, when it differs from source_url
- last_error — reason for the latest failed check
- failure_count — consecutive failures; drives exponential backoff (30 min doubling, 24 h cap)
- gone_checks — consecutive 404/410 or discontinued answers; two, at least 6 hours apart,
  make the offer unavailable
- missing_since — first time a complete category listing no longer showed the offer

## PriceObservation
- id
- offer_id
- price_amount
- currency
- observed_at
- source_fetched_at

Insert a new observation when price or currency changes. Periodic observations can be added later for price-history analytics.

## CollectionRun
- run_id
- source_key
- market_code
- started_at, finished_at
- status
- records_discovered, records_parsed, records_seen, records_valid, records_rejected, records_upserted, records_deduplicated
- error_summary and completeness as JSON text
- adapter_version

## Data rules
- Never replace unknown values with guesses.
- Store money as Decimal (or integer minor units with explicit currency conventions), not binary float.
- Do not assume all returned prices are HUF merely because the run targets Hungary.
- Preserve source identity and collection timestamp.
- Availability is unknown when not supplied or not reliably checked.
- Image URL availability and image rights are separate questions.

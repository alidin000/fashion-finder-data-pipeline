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
- category and category_path
- description — nullable
- price_amount — Decimal, nullable
- currency — ISO currency code, nullable if not confidently known
- original_price_amount — nullable
- image_urls — JSON array, subject to source usage/storage decisions
- colour_names — JSON array
- size_options — JSON array
- availability_status — in_stock, out_of_stock, unknown
- market_code
- source_fetched_at
- created_at, updated_at, last_seen_at
- status — active, needs_recheck, unavailable

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
- records_seen, records_valid, records_rejected, records_upserted
- error_count, error_summary
- adapter_version

## Data rules
- Never replace unknown values with guesses.
- Store money as Decimal (or integer minor units with explicit currency conventions), not binary float.
- Do not assume all returned prices are HUF merely because the run targets Hungary.
- Preserve source identity and collection timestamp.
- Availability is unknown when not supplied or not reliably checked.
- Image URL availability and image rights are separate questions.

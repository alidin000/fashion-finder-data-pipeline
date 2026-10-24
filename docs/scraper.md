# Scraper: discovery, refresh and the ingest API

How Fashion Finder collects Hungarian offers politely, keeps them fresh, and runs the
same refresh command on a laptop, a Render cron job, a self-hosted runner or GitHub
Actions. Written 2026-10-24. Measured numbers come from the small live run recorded
in [live source status](live-source-status.md).

Contents: [architecture](#architecture) · [run it](#run-discovery-and-refresh) ·
[add a source](#add-a-source) · [compliance](#compliance-model) ·
[reports](#reports-and-monitoring) · [benchmark](#extraction-benchmark) ·
[deployment](#deployment) · [GitHub Actions](#scheduled-refresh-on-github-actions) ·
[photos and feeds](#photos-and-feeds)

## Architecture

**Discovery and refresh are separate commands and code paths.**

| | Discovery: `fashion-finder collect` | Refresh: `fashion-finder refresh` |
|---|---|---|
| Finds | new product URLs | nothing new; re-checks known offers |
| Starts from | category listings (with pagination) or sitemaps | each due offer's stored `refresh_url` (its category page) or `source_url` (its product page) |
| Picks work by | listing order; sitemap `lastmod`, newest first; skips URLs already stored | `next_refresh_at`, most overdue first, grouped by the URL that refreshes them |
| Writes | new offers, first refresh staggered over 6–12 h | changed fields, or only the check times when nothing changed |

```text
source_definitions.toml ──► collectors.ProductSpider ──► structured_data / LPP listing / H&M cards
   (config per shop)            │  politeness middlewares (robots, TDMRep, spacing, stops)
                                ▼
                     records (RawProductRecord)
                 ┌──────────────┴───────────────┐
        local mode│                      API mode │ (FASHION_FINDER_API_URL)
                  ▼                               ▼
   scheduler.apply_batch (same rules)   POST /api/v1/ingest/offers ─► scheduler.apply_batch
                  ▼                               ▼
          catalogue database (Django: RetailerOffer + PriceObservation + CollectionRun)
```

| Module | Responsibility |
|---|---|
| `source_definitions.toml`, `sources.py` | Validated per-shop config: hosts, start pages, sitemaps, pagination, extraction overrides, refresh strategy and interval, rendering, concurrency, status. |
| `politeness.py` | robots.txt (RFC 9309 statuses, Crawl-delay, Request-rate), TDMRep, per-host spacing and concurrency cap, Retry-After backoff, stops on 401/403/challenges, request budgets, telemetry. |
| `collectors.py` | The Scrapy spider: listings, pagination, sitemaps, product pages, refresh and benchmark modes; a separate Playwright crawler only for `rendering = "browser"` shops. |
| `structured_data.py` | schema.org Product/ProductGroup/Offer from JSON-LD, microdata, OpenGraph and configured embedded state JSON; variant grouping. |
| `normalisation.py`, `category_mapping.py`, `image_selection.py` | HUF prices, availability, colours, categories, photo choice. |
| `discovery.py` | Sitemap and sitemap-index parsing (gzip, `lastmod`), child rotation, refresh priorities, pagination rules. |
| `scheduler.py` | Due queue on the offer table, refresh rules, removal confirmation, capacity and interval stretching, run rows. |
| `runner.py` | Refresh and API-mode discovery; database or API backend; cycle report and Markdown job summary. |
| `api/ingest.py` | Token-authenticated `due`, `known` and `offers` endpoints. |
| `crawl_state.py` | Local cache only: ETag/Last-Modified validators, TDMRep files, resumable discovery runs. |
| `benchmark.py` | Per-retailer extraction benchmark. |
| `replay.py` | Serves recorded responses instead of the network (tests, offline rehearsals). |

### Data model (migration `0004_offer_refresh_schedule`)

One additive migration adds the refresh state to the existing `RetailerOffer`; there is
no parallel model, and the unique `(retailer, source_product_id)` and
`(retailer, canonical_url)` constraints are unchanged.

| Field | Meaning |
|---|---|
| `last_checked_at` | Last successful or failed check. Shoppers see `last_checked_at` in the API as before (it is `source_fetched_at`, which an unchanged check also advances). |
| `next_refresh_at` (indexed, plus `(retailer, next_refresh_at)`) | When the offer is due. |
| `last_error`, `failure_count` | Last failure reason and the consecutive-failure count that drives backoff. |
| `gone_checks`, `missing_since` | Removal evidence: HTTP 404/410 or discontinued answers, and when it started. |
| `refresh_url` | The page that refreshes this offer: its category listing for listing-strategy shops (LPP), else empty (the product page). |

### Refresh rules (`scheduler.py`)

- **Success, something changed:** full update; a price change also adds a
  `PriceObservation`. Next check one interval later.
- **Success, nothing changed (or HTTP 304):** only `last_checked_at`,
  `source_fetched_at`, `last_seen_at`, `next_refresh_at` and the failure fields are
  written. No full-row save, no price observation.
- **Failure:** `failure_count + 1`, next check after 30 min, 1 h, 2 h ... capped at 24 h.
- **HTTP 404/410, a redirect to a non-product page, or `Discontinued`:** recheck after
  6 h; the offer becomes `unavailable` (hidden from shoppers) only after two such
  answers at least 6 h apart. Unavailable offers are rechecked weekly and come back as
  soon as a check finds them. Nothing is deleted.
- **Missing from a complete category listing:** never removed for that alone. The next
  check, 6 h later, fetches the offer's own product page.
- **New offers:** first refresh at 50–100 % of the interval (a stable hash of the URL),
  so one big discovery run does not make everything due at once.
- **Unattempted work** (time budget, request budget, a stopped shop) stays due.

### Concurrency and the 12-hour target

- One request per host at a time by default (`max_concurrency`, 1–4 per shop), and
  request *starts* are spaced by the largest of `--delay` (default 10 s, minimum 3 s),
  robots.txt `Crawl-delay`/`Request-rate`, and any `Retry-After` backoff. AutoThrottle
  can only slow this down further.
- Different shops run in parallel; each has its own spacing and its own request budget,
  so no shop can monopolise a run.
- **Stretching:** before reporting, each refresh computes
  `units × requests per unit × spacing / 0.8`. If that exceeds the shop's interval
  (default 12 h, `refresh = { interval_hours = ... }`), the interval is stretched and the
  cycle report says so (`stretched: true`, with the reason). The crawler never goes
  faster than the limits to meet the target.
- Example: a product-page shop with 12,000 offers and `Crawl-delay: 10` needs 33.3 h
  (12,000 × 10 s / 0.8), so it is refreshed every 33.3 h, not every 12. The same
  12,000 LPP offers refresh through ~28 category listings: 56 requests, 0.2 h.

## Run discovery and refresh

Install once: `python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'`.

```bash
# Discovery into the local catalogue (data/fashion_finder.sqlite)
fashion-finder collect reserved --limit 300            # default category, LPP listing
fashion-finder collect all --limit 1000 --all-categories  # every reviewed shop, curated categories
fashion-finder collect sportano --mode sitemap --max-sitemaps 2 --limit 100   # candidate, opt-in
fashion-finder collect modivo --url https://modivo.hu/c/<category-path> --limit 50  # needs a start URL
fashion-finder collect reserved --resume              # continue an interrupted discovery run

# Refresh due offers (default: every reviewed shop, 500 offers, 100-minute budget)
fashion-finder refresh
fashion-finder refresh --source reserved --limit 2000 --time-budget-minutes 240

# Monitoring
fashion-finder cycle-report            # queue health per shop
fashion-finder runs --limit 20         # discovery runs and refresh cycles
fashion-finder quality                 # field coverage, invalid rows and reasons per shop
```

Common options: `--max-requests N` (budget per shop, robots.txt included),
`--delay S`, `--contact-url URL` (or `FASHION_FINDER_CONTACT_URL`),
`--time-budget-minutes M` (stop cleanly, leave the rest due), `--api-url URL` (see below).
`collect all` runs only `status = "reviewed"` shops; blocked and candidate shops run only
when named. `--browser` remains a one-off diagnostic override; scheduled runs use
Playwright only for shops marked `rendering = "browser"` (none today).

**API mode** (no local catalogue; the runner keeps nothing):

```bash
export FASHION_FINDER_API_URL=https://<your-api-host>
export FASHION_FINDER_API_TOKEN=<token>        # never pass it as a flag
fashion-finder refresh --limit 500 --time-budget-minutes 100
fashion-finder collect all --limit 300         # discovery, results POSTed in batches of 500
```

The runner pulls due offers from `GET /api/v1/ingest/due`, refreshes them through the
same polite crawler, and pushes results to `POST /api/v1/ingest/offers`. Its crawl
cache lives in a temporary directory deleted at exit. When `$GITHUB_STEP_SUMMARY` is
set, the cycle report is appended there as Markdown.

### The ingest API

| Route | Use |
|---|---|
| `GET /api/v1/ingest/due?source=&limit=` | Due offers (id, product URL, check URL and kind, price, sizes, `next_refresh_at`, failures) plus queue stats. Batched query, at most 5,000. |
| `GET /api/v1/ingest/known?source=&after=&limit=` | Stored canonical URLs, paged by id, so discovery fetches only new products. |
| `POST /api/v1/ingest/offers` | `{"run_id", "intervals", "items": [...], "cycle_report"}`; at most 500 items and 4 MB. Items are `observation` (a `RawProductRecord`), `not_modified`, `failure` (`gone` for 404/410/discontinued) or `missing`. Each item is validated with the repo's schema models and applied on its own: per-item `created`, `updated`, `unchanged`, `recorded` or `rejected` with a reason. |

- Auth: `Authorization: Bearer <token>`, compared with `FASHION_FINDER_INGEST_TOKEN` by
  `hmac.compare_digest`. If the variable is unset or shorter than 32 characters, every
  ingest call answers **503**; there is no open fallback.
- CSRF-exempt only because requests carry no cookies and are bearer-authenticated.
- Per-process throttle (120 GET and 60 POST per minute per token, answer 429 with
  `Retry-After`), body limit 4 MB (413), batch limit 500 items (413).
- Audit: each batch writes one `CollectionRun` row per shop (`adapter_version =
  api-ingest-v1`) with the token's fingerprint (`ingest-token:<sha256 prefix>`, never the
  token), the client run id, the runner's User-Agent and the counts; plus a log line on
  `fashion_finder_data.ingest.audit`. Cycle reports are stored as `refresh-cycle-v1` rows.
- Ingest never approves publishing: source policies still decide what shoppers see.

## Add a source

1. Read the shop's robots.txt and terms (see [compliance](#compliance-model)); record the
   evidence in [Hungarian source candidates](hungarian-source-candidates.md).
2. Add a table to `src/fashion_finder_data/source_definitions.toml`:

```toml
[sources.exampleshop]
name = "Example Shop"
host = "example-shop.hu"
hosts = ["example-shop.hu", "www.example-shop.hu"]   # exact product hosts (optional)
status = "candidate"                  # never "reviewed" until a live check is documented
market_paths = ["/"]                  # the whole domain is Hungarian
category_url = "https://example-shop.hu/noi/kabatok"   # discovery start (optional)
product_pattern = '^https://example-shop\.hu/termek/'  # which links are product pages
pagination = { type = "query", param = "page", max_pages = 20 }   # or "rel-next"
sitemaps = ["https://example-shop.hu/sitemap.xml"]     # from robots.txt (optional)
sitemap_follow = "product"            # regex for child sitemaps to follow
extraction = { title_suffix = '\s*\|\s*Example Shop$', type_aliases = { "Termék" = "Product" } }
refresh = { strategy = "product", interval_hours = 12 }
max_concurrency = 1
```

   Optional `extraction` keys: `listing_structured_data = true` (store complete products
   found in a category page's ItemList without product requests) and `state` for embedded
   state JSON, e.g.
   `state = [{ script_id = "__NEXT_DATA__", product_path = "props.pageProps.product",
   fields = { name = "name", sku = "sku", price = "price.final", currency = "price.currency",
   sizes = "sizes[].label", images = "images[].url" } }]`.
3. Add a **pending** entry to `config/source-policies.example.json`.
4. Add a fixture-backed test (see `tests/test_structured_data.py`), run
   `fashion-finder benchmark-extraction --source exampleshop --per-source 5`, and only
   after a documented live check set `status = "reviewed"`.

A shop that publishes schema.org Product data needs no Python code. Code is still needed
for a new platform listing format (like LPP's embedded catalogue or H&M cards), unusual
embedded JSON that the `state` paths cannot express, or a new pagination scheme.

## Compliance model

- **robots.txt** via Protego with the product token `FashionFinderBot`. Status rules from
  RFC 9309: 2xx parsed; 404/410 and other 4xx mean no restrictions; 401/403, 429, 5xx,
  network errors and challenge pages mean "disallow all" and stop the shop.
  `Crawl-delay` and `Request-rate` widen the spacing; a Crawl-delay above 600 s stops
  the run for that shop. Every request that waits for robots.txt gets the real rules:
  Scrapy's stock middleware shares one Twisted Deferred, and `Deferred.asFuture` hands
  `None` (meaning "allowed") to every waiter but the first, so concurrent requests could
  skip the check. `PoliteRobotsMiddleware` uses an asyncio future instead
  (`tests/test_politeness.py::test_robots_rules_and_crawl_delay_are_obeyed`).
- **Honest User-Agent:** `FashionFinderBot/0.1.0 (+<contact URL>)`. Set
  `FASHION_FINDER_CONTACT_URL` to a public page with the project description and an
  impresszum once it exists; the default is the repository URL.
- **Text-and-data-mining opt-outs:** `/.well-known/tdmrep.json` is read per host (cached
  7 days) before any page; a reservation, an unreadable file, a `tdm-reservation: 1`
  header or a `noai` robots meta/X-Robots-Tag stops the shop. `noimageai` keeps the offer
  but drops its photos. Photo fetching keeps its own TDMRep, header and robots checks.
- **Blocks:** HTTP 401/403 twice stops the shop; any CAPTCHA or bot-challenge page
  (Cloudflare, Akamai, PerimeterX, DataDome, Imperva, Distil markers, or "captcha" on a
  block status or a tiny page) stops it at once; 429/503 back off with `Retry-After`
  (seconds or HTTP date), retry at most twice, double the host spacing, and stop after
  three rate limits or a Retry-After above 300 s. A stopped shop is reported, not retried.
- **No evasion:** no proxies, rotation, stealth, spoofed headers or fingerprints, CAPTCHA
  solving, logins or account-only pages. Cookies are disabled. GitHub-hosted runners use
  shared datacenter IP addresses: if a retailer blocks them, the run records the block and
  stops; never route around it.
- **Budgets:** `--max-requests` per shop counts every request including robots.txt;
  `--time-budget-minutes` stops cleanly.
- **Publication:** no command approves a source. Policies stay pending until the owner
  records evidence; ingest only stores.

## Reports and monitoring

- **Discovery run report** (`data/reports/<run_id>.json`, also printed): the existing
  counts plus `offers` (new/changed/unchanged), `field_coverage` (title, price, currency,
  original price, image, brand, description, colour, size, sku, known availability, mapped
  category), `rejection_reasons`, and `crawl`: requests, status codes, requests by kind,
  bytes, 304s, robots-disallowed, backoffs, stop reason, budget, errors by reason, crawl
  delays, latency p50/p95, duration, complete listings.
- **Refresh cycle report** (printed, Markdown to `$GITHUB_STEP_SUMMARY`, stored as
  `refresh-cycle-v1` rows): per shop, due at start, checked, results, failures by reason,
  removal progress, due remaining (`cycle_complete`), requests, status mix, latency,
  stop reason, configured and effective interval with the stretch reason, and offers
  older than 1.5× the interval.
- `fashion-finder cycle-report`: live queue per shop (active, due, never checked, stale
  over 1.5× interval, failing, pending removal, unavailable, interval, last cycle).
- `fashion-finder runs`: recent discovery runs, ingest batches and refresh cycles.
- `fashion-finder quality`: the existing freshness checks plus `sources.<key>` with field
  coverage, status counts and the latest run's invalid rows and reasons.

## Extraction benchmark

`fashion-finder benchmark-extraction` samples N product pages per shop (stored offers
spread across the catalogue, or pages linked from the first listing or sitemap), fetches
them through the polite crawler and reports the method (feed/API, embedded JSON, JSON-LD,
microdata, OpenGraph, HTML selectors, needs JavaScript), success rate, price and size
coverage, latency p50/p95, HTTP status mix, whether JavaScript looked necessary, the
cycle time at the allowed rate and the concurrency a 12-hour cycle needs (or why none
can reach it within Crawl-delay).

Measured on 2026-10-24 (8 pages per shop, live): Reserved, Sinsay and Mohito product
pages give JSON-LD with price on 8/8 pages but **no sizes**; the category listing's
embedded JSON gives sizes and per-size stock for every offer. That is why LPP shops
refresh through their listings. Full numbers: [live source status](live-source-status.md).

The full benchmark (owner's plan: 100–500 pages) is for the owner to start, from the
deployment network, after discovery has stored offers:

```bash
fashion-finder benchmark-extraction --per-source 50 --max-requests 60 \
  --source reserved --source sinsay --source mohito --source house --source cropp \
  --output data/reports/benchmark-$(date -u +%Y%m%d).json        # 250 pages
```

Candidate shops (`sportano`, `ecipo`, `modivo`) can be added with `--source` once their
terms are reviewed; at a 10 s spacing 50 pages take about 9 minutes per shop.

## Deployment

Production should use a managed PostgreSQL database. The scheduler and ingest code use
only portable ORM queries (no raw or SQLite-only SQL), aware `DateTimeField`s, one
transaction per item, and no row locks because one worker applies results. The project's
`settings.py` is still SQLite-only (its `OPTIONS` are SQLite pragmas); switching the
database is a separate change. The local `crawl-state.sqlite` is only a cache.

### Option A: a Render cron job (proposed, not applied)

The runner talks to the API, so the cron job needs no database access. Note that the
current `render.yaml` API runs the synthetic demo with a temporary `/tmp` database; point
a refresh job only at an API with a persistent database.

```yaml
  - type: cron
    name: fashion-finder-refresh
    runtime: docker
    region: frankfurt
    dockerfilePath: ./Dockerfile.api
    dockerContext: .
    schedule: "17 6,18 * * *"        # UTC; every 12 hours
    dockerCommand: fashion-finder refresh --limit 2000 --time-budget-minutes 300
    envVars:
      - key: FASHION_FINDER_API_URL
        value: https://fashion-finder-api.onrender.com
      - key: FASHION_FINDER_API_TOKEN
        sync: false                  # set in the dashboard; same value as the API's ingest token
      - key: FASHION_FINDER_CONTACT_URL
        sync: false
```

Check the snippet against Render's Blueprint reference before merging. A run exits when
due work is done or the time budget ends, so a cycle never overlaps the next.

### Option B, later: Celery and Redis

Designed for, not built: a beat task enqueues per-shop refresh batches (one queue per
shop, worker concurrency 1 per queue keeps the per-host spacing); workers claim due rows
with `select_for_update(skip_locked=True)` in `scheduler.due_offers`, run the same
`ProductSpider` in refresh mode in a subprocess (Scrapy's reactor cannot restart), and
apply results with `scheduler.apply_batch`. Browser shops get their own low-concurrency
queue. Move only when cycle reports show the window at risk.

## Scheduled refresh on GitHub Actions

`.github/workflows/fashion-finder-refresh.yml` runs `fashion-finder refresh` in API mode
at 08:17 and 20:17 Budapest time, plus manual runs (`workflow_dispatch` with `command`,
`limit`, `source`). It uses the same action versions as `ci.yml`, pip caching,
`permissions: contents: read`, a non-cancelling concurrency group and a 120-minute
timeout; the run itself stops after 100 minutes. Chromium is installed only if a selected
shop is marked `rendering = "browser"`. Nothing is committed or uploaded; only the job
summary is written.

### Setup

1. Create a token: `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`.
2. On the API host (Render), set `FASHION_FINDER_INGEST_TOKEN` to it and redeploy.
   Use an API with a persistent database.
3. In the GitHub repository: **Settings > Secrets and variables > Actions**, add the
   secrets `FASHION_FINDER_API_TOKEN` (the same token) and `FASHION_FINDER_API_URL` (the
   API's HTTPS base URL). Optionally add the variable `FASHION_FINDER_CONTACT_URL`.
4. Start a first manual run: **Actions > Fashion Finder product refresh > Run workflow**,
   `command = discover`, `limit = 300`. Then a `refresh` run. Read the job summary.

Without the secrets (forks, or before setup) the job prints a notice and ends green.
`fashion-finder refresh` exits 0 when it finished its budget, even if a shop was stopped
(the stop appears in the summary and as a workflow warning, so a blocked retailer does
not turn every run red); it exits 1 when the API cannot be reached or rejects the token.

### Cost

| Repository | Runner minutes |
|---|---|
| Public, standard GitHub-hosted runner | Free |
| Private, GitHub Free plan | 2,000 minutes a month included; 60 runs a month leaves about 33 minutes a run |

(From the owner's brief, 2026-10-24.) Start with a few hundred offers and grow the
`limit` on measured completion time.

### Schedule caveats

- Scheduled runs can be delayed under load, especially at the top of the hour (hence :17).
- They run only from the default branch's latest commit.
- In a public repository they are disabled after 60 days without repository activity.

### GitHub's terms

GitHub's terms for Actions
(https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features)
say Actions should not be used:

- "as part of a serverless application";
- on GitHub-hosted runners, for "any other activity unrelated to the production,
  testing, deployment, or publication of the software project";
- for "any activity that places a burden on our servers, where that burden is
  disproportionate to the benefits provided to users".

A production data refresh is a grey area: acceptable for a small demo, but keep it
portable. The identical command runs elsewhere:

- **Render cron job:** the snippet above (`fashion-finder refresh ...` with the same two
  environment variables).
- **Self-hosted runner:** register a runner and change `runs-on: ubuntu-latest` to
  `runs-on: self-hosted`; nothing else changes.
- **Any server:** a cron entry such as
  `17 6,18 * * * cd /srv/fashion-finder && FASHION_FINDER_API_URL=... FASHION_FINDER_API_TOKEN=... .venv/bin/fashion-finder refresh --limit 2000`.

## Photos and feeds

- `fashion-finder fetch-images` keeps every policy, robots and TDMRep check and now: honours
  the image host's robots `Crawl-delay`; spaces requests per host and interleaves hosts;
  treats TDMRep 3xx answers without a target as "no file" (the LPP 301 bug); skips photos
  that send `tdm-reservation: 1` or `X-Robots-Tag: noai/noimageai`; picks the best
  `--per-offer` photos (packshot hints first, size charts, logos, swatches, banners and
  vector art dropped); stores identical bytes once; and `--revalidate-days N` re-checks
  older photos with ETag/Last-Modified without rewriting unchanged files.
- `map-xml-feed` accepts ordered fallback paths (`"price": ["sale_price", "price"]`),
  `additional_image_urls`, `item_group_id` (size variants of one link become one row),
  a record `filter`, gzip and streaming input up to 512 MB, and `--allow-partial`.
  Affiliate and partner feeds remain the preferred route where they exist
  ([affiliate feeds](data-research/affiliate-feeds.md)).

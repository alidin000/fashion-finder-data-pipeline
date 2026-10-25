# Fashion Finder

Fashion Finder is a clothing discovery app for Hungarian shops. A polite product
collector gathers current offers, a Django API serves them, and a Next.js page lets
shoppers search three ways: **words and filters**, **match by look** (describe it, in
Hungarian or English) and **photo search** (upload a photo, find similar items).

**Live demo:** <https://fashion-finder-web.onrender.com>. It runs on Render's free
plan: the first visit wakes it (about a minute), and its catalogue is refilled by a
scheduled run ([hosted demo](#hosted-demo)). It has word and photo search; match by
look runs locally, on real products or on the licence-safe practice catalogue.

This is a working prototype, not a public shopping service. What each shop's products
may show is an owner decision recorded in a source policy; the repository's default
policy publishes nothing.

## What it looks like

![The landing page: headline, search panel and photo panel](docs/images/design-home.jpg)

**Match by look, in Hungarian.** "piros női cipő" is read as *red women's shoes*; the
page says how it read the query and what it ranked for.

![Look search results with the "Searching for" line](docs/images/design-look-search.jpg)

**Photo search.** The app picks the region to compare (here it was unsure, so it says
so and offers a manual crop), names what it saw, and ranks similar items.

<table>
  <tr>
    <td valign="top" align="center"><img src="docs/images/design-photo-panel.jpg" alt="Photo panel with the auto-focus box and the understood category" width="330"><br><sub>Desktop photo panel</sub></td>
    <td valign="top" align="center"><img src="docs/images/design-mobile.jpg" alt="Photo search on a 390 px phone screen" width="260"><br><sub>The same on a phone</sub></td>
  </tr>
</table>

![Photo search results: white canvas sneakers first](docs/images/design-photo-results.jpg)

Screenshots use the [practice catalogue](docs/practice-catalogue.md)'s ABO products
(photos © Amazon.com, CC BY 4.0) and a CC0 query photo ([Converse Shoes](https://commons.wikimedia.org/wiki/File:Converse_Shoes.jpg)
by Wilfredor). Real shop photos are never stored in this repository.

## What a shopper can do

1. Search by words, category, colour, size, shop and maximum HUF price, in Hungarian
   or English, with or without accents. `fekete kabát 30000 Ft alatt` fills the price
   limit; who an item is for (`női`, `men's`) ranks but never filters, because offers
   carry no gender field.
2. Tick **Match by look** to rank product photos by the description itself. The page
   shows how the query was read ("Searching for: black women's coat (from 'fekete női
   kabát')"), plus price and size chips.
3. Upload a photo. The app focuses on the main garment, draws the box it used, names
   what it saw ("Jacket · black"), offers other garments found in the photo, and lets
   you crop by hand or use the whole photo. Colour variants fold into a "+N colours"
   badge.
4. Sort by price or last check and load more. Each card shows the shop, HUF price,
   when the price was last checked (with a warning after 18 hours) and the stock claim.
   Visual results are labelled **Style inspiration**: similar is not identical.
5. Open the offer on the shop's Hungarian website to confirm the details.

There is no account, checkout, delivery estimate, stock guarantee or price alert. A
shopper's search never starts a crawl; it reads stored data only.

## Design

### Visual design

An editorial, fashion-magazine feel: a serif display face for headlines, a warm
off-white background, coral accents for actions, and a calm sage panel for photo
search. Results are a five-column card grid on desktop and a single column on a
phone, where every section stacks. Each card leads with the photo, then the shop, the
title, the HUF price and a quiet line about freshness and stock. The page always says
what it did: the box it compared, what it understood (with a "?" when unsure), how a
Hungarian query was read, and labels for practice or demo items.

### System design

```mermaid
flowchart LR
  subgraph Collect["Collection (local, or GitHub Actions twice a day)"]
    A[collect: discovery] --> N[Normalise and validate]
    R[refresh: due offers] --> N
    F[Partner feed CSV/XML] --> N
  end
  N --> D[(SQLite: offers, prices, runs, refresh queue)]
  N -->|token-protected ingest API| D
  D --> I[fetch-images, index-images, index-product-text]
  I --> X[(Photo index and product-text index)]
  P[Source policy: listing, photo display, photo processing] --> API
  D --> API[Django /api/v1]
  X --> API
  API --> W[Next.js shopper page]
```

- **Collection:** `collect` discovers products on the shops marked `reviewed` in
  `src/fashion_finder_data/source_definitions.toml`; `refresh` re-checks known offers
  when they fall due, without crawling the whole site. Both honour robots.txt
  (including Crawl-delay), text-and-data-mining opt-outs and per-host pacing, send an
  honest `FashionFinderBot` User-Agent, and stop on 401/403 or a CAPTCHA instead of
  working around it. See the [scraper guide](docs/scraper.md).
- **Storage:** raw JSONL is kept, records are normalised, deduplicated by source
  product ID or URL, and price changes and run errors are recorded in SQLite.
- **Publication:** a default-deny source policy controls listing, photo display and
  photo processing separately. The API hides prohibited, stale, out-of-stock, non-HUF
  or foreign-link offers; unknown stock stays unknown.
- **Serving:** Django returns bounded, cursor-paged JSON; the page talks to it through
  its own same-origin proxy and never sees raw records or policy evidence.

### How photo search works

```mermaid
flowchart LR
  U[Shopper photo] --> B[Browser: apply rotation, drop metadata, max 1024 px, optional crop]
  B --> F[Auto focus: whole photo plus 7 regions, scored against garment and person prompts]
  F --> E[Marqo-FashionSigLIP embedding, INT8 ONNX]
  E --> K[Nearest indexed photos, one matrix product]
  K --> P[Policy, freshness and filters per offer]
  P --> V[Category vote plus zero-shot category and colour]
  V --> C[Fold colour variants]
  C --> O[Results and what was understood]
```

The model is [Marqo-FashionSigLIP](https://huggingface.co/Marqo/marqo-fashionSigLIP)
(Apache-2.0), pinned by SHA-256 and run on CPU with ONNX Runtime; uploads stay in
memory. Auto focus needs the text model too; without it the whole photo is compared.

### How match by look works

```mermaid
flowchart LR
  Q[Hungarian or English words] --> H[Read the query: 476-entry dictionary, suffixes, compounds, price, size, colour, gender]
  H --> T["English text: 'a photo of red women's shoes'"]
  T --> E[FashionSigLIP text embedding]
  E --> S1[Similarity to product photos]
  E --> S2[Similarity to the product-text index]
  S1 --> B[Blend]
  S2 --> B
  B --> R[Fuse with word matches, reciprocal rank fusion]
  R --> O[Results and 'Searching for ...']
```

An optional pinned offline translator (opus-mt-hu-en) handles words the dictionary
does not know. Colour, category and gender boost results; price and size filter them.

### Measured results

On the practice catalogue (3,387 products, held-out queries, INT8 models); details and
commands in [benchmark results](docs/benchmark-results.md).

| Task | Before | Now |
| --- | --- | --- |
| Photo of an item → same category in the top 5 (Hit@5) | 0.937 | 0.953 |
| Garment inside a street photo → that item first (Hit@1) | 0.35 | 0.60 |
| Hungarian phrase, match by look (MRR@10) | 0.876 | 0.958 |
| Same phrases in English (MRR@10) | 0.791 | 0.936 |
| Photo search time, 95th percentile (laptop) | | 1.2 s |

Auto focus costs about two points on clean product photos (exact item in the top 5:
0.702 → 0.685) and wins far more on street photos. The exact-item case for a real
person wearing the product is not measured: no licence-safe test set exists.

### API routes

| Route | Purpose |
| --- | --- |
| `GET /api/v1/health` | Service status, latest catalogue timestamp, photo and look capability, analytics setting, demo mode. |
| `GET /api/v1/search` | Eligible offers with text, filters, sort, limit and cursor. |
| `GET /api/v1/offers/{id}` | One current, permitted offer or 404. |
| `GET /api/v1/search/look` | Match by look; returns `interpreted_query` and `interpretation` (language, translation, colour, category, size, price limits). |
| `POST /api/v1/search/image` | Photo search over an enabled photo index (or the five demo samples); returns what it understood in `query`. Uploads: up to 5 MB and 2048×2048 px; the page sends 1024 px. |
| `POST /api/v1/events` | Coarse daily metrics, only when analytics is enabled. |
| `GET /api/v1/ingest/due`, `GET /api/v1/ingest/known`, `POST /api/v1/ingest/offers` | Token-only API for a refresh runner (503 unless `FASHION_FINDER_INGEST_TOKEN` is set); never publishes by itself and refuses demo and practice rows. |
| `GET /api/v1/ingest/photos`, `POST /api/v1/ingest/image-index` | Same token. A runner builds the photo index: it asks which permitted photos are missing, then uploads their vectors (never the photos). |

### Where the code lives

| Path | Responsibility |
| --- | --- |
| `web/app/`, `web/lib/` | Shopper page, photo crop and upload, look-search display. |
| `src/fashion_finder_data/collectors.py`, `discovery.py`, `structured_data.py`, `politeness.py` | Discovery crawls, schema.org and embedded-JSON extraction, robots and pacing. |
| `src/fashion_finder_data/source_definitions.toml`, `sources.py` | Every shop as validated configuration (hosts, start pages, sitemaps, status). |
| `src/fashion_finder_data/scheduler.py`, `runner.py`, `api/ingest.py` | Refresh queue, backoff, removal rules and the API-mode runner. |
| `src/fashion_finder_data/image_fetch.py`, `image_index.py`, `query_understanding.py`, `visual_encoder.py` | Photo download, photo index, auto focus, zero-shot reading and the pinned models. |
| `src/fashion_finder_data/photo_runner.py`, `hosted_index.py` | `index-photos`: a runner encodes permitted photos and uploads vectors; the API checks and merges them into the photo index. |
| `src/fashion_finder_data/text_match.py`, `hungarian.py`, `look_query.py`, `look_search.py`, `product_text.py` | Hungarian word handling, query reading and look-search ranking. |
| `src/fashion_finder_data/practice/` | Licence-safe practice catalogue and the search benchmark. |
| `src/fashion_finder_data/feed.py`, `refresh.py` | Partner feed import and the feed worker. |
| `src/fashion_finder_data/schema.py`, `pipeline.py`, `storage.py` | Validation, normalisation, raw archive and SQLite writes. |
| `src/fashion_finder_data/public_catalogue.py`, `api/` | Policy-aware search and Django HTTP routes. |
| `src/fashion_finder_data/catalogue/` | Django models, migrations and read-only staff admin. |
| `config/source-policies.*.json` | The example (all pending) and the hosted demo's policy. |
| `tests/`, `web/lib/*.test.ts`, `.github/workflows/` | Tests, CI, and the scheduled refresh. |

## Run it

### Run the app locally with Docker

From the repository root in Bash or WSL, with Docker Compose installed:

```bash
cp .env.example .env
python3 -c 'import secrets; print("FASHION_FINDER_SECRET_KEY=" + secrets.token_urlsafe(48))' >> .env
mkdir -p data/feeds
cp config/source-policies.example.json data/source-policies.json
docker compose up --build -d
docker compose ps
curl -fsS http://127.0.0.1:8000/api/v1/health
```

Open **http://localhost:3000**. The API listens on
**http://127.0.0.1:8000**. Both ports bind to loopback. On Linux or WSL, set
`FASHION_FINDER_UID` and `FASHION_FINDER_GID` in `.env` to `id -u` and `id -g`
if they differ from 1000. Keep `.env` and the source policy private.

The example policy is deliberately **pending**, so a fresh install shows an
empty catalogue. Do not mark a shop approved merely to fill the UI; approving
for local evaluation is a separate, recorded owner decision (next section). Follow the
[local Docker runbook](docs/local-docker-runbook.md) to import a permitted feed,
inspect worker logs, back up SQLite, and check the app. The
[local Python/Node setup](docs/app-local-setup.md) covers development without
Docker.

For a populated **synthetic local demo**, run `bash scripts/start_local_demo.sh`
and open `http://127.0.0.1:3300`. The [demo guide](docs/local-demo.md) explains
its separate database, sample labels, and limits. Demo products never count
toward the real-source release gates.

### Run it on real products (local evaluation)

These are the steps of the 25 October 2026 run, from the repository root in Bash
or WSL. Times are what that run took on a laptop.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev,image-model]'
.venv/bin/fashion-finder --data-dir data migrate

# 1. Collect (about 7 minutes: 137 requests, at least 10 s apart per shop)
.venv/bin/fashion-finder --data-dir data collect all --all-categories \
  --limit 1000 --max-requests 40 --time-budget-minutes 30
```

**2. Decide what may be shown.** Copy `config/source-policies.example.json` to
`data/source-policies.json`. For each shop you choose to show, set
`approval_status` to `approved`, the three `allow_*` flags to `true`, and an
`evidence_ref` that records the decision honestly, for example `"local evaluation
only: owner decision 2026-10-25; not for public deployment"`. Collecting data is
not permission to publish it: never deploy a policy approved this way. A public
deployment needs real permission or an affiliate feed per shop.

```bash
# 3. Photos: one per offer, shops taken in turn. Most image hosts ask for
#    10 s between requests, so 1,500 photos took about 50 minutes.
.venv/bin/fashion-finder --data-dir data fetch-images \
  --policy data/source-policies.json --limit 1500 --per-offer 1

# 4. Models: the pinned INT8 Marqo-FashionSigLIP files (links and SHA-256 values
#    in docs/local-image-index.md) saved as:
#    data/models/fashion-siglip-vision-int8.onnx, data/models/fashion-siglip-text-int8.onnx,
#    data/models/fashion-siglip-tokenizer.json

# 5. Indexes (about 9 minutes for 1,500 photos, then about 1 minute)
M=data/models
.venv/bin/fashion-finder --data-dir data index-images data/images/manifest.json \
  --policy data/source-policies.json --model-path $M/fashion-siglip-vision-int8.onnx
.venv/bin/fashion-finder --data-dir data index-product-text \
  --policy data/source-policies.json --model-path $M/fashion-siglip-vision-int8.onnx \
  --text-model-path $M/fashion-siglip-text-int8.onnx \
  --tokenizer-path $M/fashion-siglip-tokenizer.json

# 6. API on 127.0.0.1:8000
FASHION_FINDER_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))') \
FASHION_FINDER_DB=$PWD/data/fashion_finder.sqlite \
FASHION_FINDER_SOURCE_POLICIES=$PWD/data/source-policies.json \
FASHION_FINDER_IMAGE_SEARCH_EXPERIMENT=1 \
FASHION_FINDER_IMAGE_MODEL_PATH=$PWD/$M/fashion-siglip-vision-int8.onnx \
FASHION_FINDER_TEXT_MODEL_PATH=$PWD/$M/fashion-siglip-text-int8.onnx \
FASHION_FINDER_TOKENIZER_PATH=$PWD/$M/fashion-siglip-tokenizer.json \
.venv/bin/python manage.py runserver 127.0.0.1:8000

# 7. Shopper page on http://localhost:3000 (second terminal, Node 22 or newer)
cd web && npm ci
FASHION_FINDER_API_INTERNAL_URL=http://127.0.0.1:8000 npx next dev -p 3000 -H 127.0.0.1
```

`GET /api/v1/health` should report `"image_search": "available"` and
`"text_to_image": "available"`. Next.js 16.4.0 may stop once on the first `next dev`
or `next build` to print a security notice; run the command again. `npm audit`
reports critical advisories fixed in 16.4.3, so upgrade before any public
deployment. To keep the data fresh, run `fashion-finder --data-dir data refresh`
(see the [scraper guide](docs/scraper.md)); rerun `fetch-images` and both index
commands afterwards so new offers get photos.

### Practice catalogue (no scraping)

`fashion-finder --data-dir <empty dir> load-practice-catalogue <practice dir>`
loads about 3,600 public-dataset products (ABO, CC BY 4.0; GLAMI-1M, Apache-2.0)
into their own data directory, never next to real data, so every search can be
tried and measured before retailers are approved. Building the practice folder,
loading it and running the page are described in the
[practice catalogue guide](docs/practice-catalogue.md); measured results for
every search option are in [benchmark results](docs/benchmark-results.md).

## Hosted demo

<https://fashion-finder-web.onrender.com> (page) and
<https://fashion-finder-api.onrender.com/api/v1/health> (API) run from `render.yaml`
on Render's free plan, in Frankfurt.

- **What it shows:** the five reviewed shops (Reserved, Sinsay, Mohito, House, Cropp):
  names, HUF prices, links to the shop, and photos loaded straight from each shop's own
  image server. This follows `config/source-policies.render-listing.json`, an owner
  decision of 25 October 2026 made without the shops' permission and recorded in its
  `evidence_ref`.
- **Search:** words and filters, and photo search. The free instance (0.1 CPU, 512 MB)
  cannot build a photo index, so the scheduled run builds it: it downloads photos
  politely, encodes them with the pinned INT8 vision model and uploads only the
  vectors. A photo search takes about 5 seconds there. Match by look is off: its text
  model does not fit in 512 MB beside the vision model.
- **Data:** the catalogue lives in temporary storage. It is empty after every deploy
  and every wake-up, and the free instance sleeps after 15 minutes without visitors.
  The [refresh workflow](.github/workflows/fashion-finder-refresh.yml) rediscovers the
  catalogue at 08:17 and 20:17 Budapest time through the token-protected ingest API,
  then rebuilds the photo index. Vectors (never photos) are kept between runs in an
  `actions/cache` embedding cache: a first run needs about 45 minutes of photo
  downloads, later runs a minute or two. Start it by hand (**Actions → Fashion Finder
  product refresh → Run workflow**) before showing the site to someone.
- **Setup once:** copy the generated `FASHION_FINDER_INGEST_TOKEN` from the Render API
  service's **Environment** page into the GitHub repository secret
  `FASHION_FINDER_API_TOKEN`, and set the secret `FASHION_FINDER_API_URL` to
  `https://fashion-finder-api.onrender.com`. Both must be **Repository secrets** under
  **Settings → Secrets and variables → Actions**. Details are under "Hosted demo" in
  the [scraper guide](docs/scraper.md).

## Data coverage and release state

**Local run, 25 October 2026.** `collect all --all-categories --limit 1000`
stored 1,000 current offers from each reviewed shop (Reserved, Sinsay, Mohito,
House, Cropp) with no rejected records: every offer has a HUF price, photo URL,
colour and sizes. Categories covered sweaters, tops, jeans, trousers, jackets,
coats and dresses; 9 % had no mapped category, and the curated lists do not yet
include shirts, skirts, shoes, bags or accessories. Cropp's women's shorts
category answered 404 and was removed from the configuration. The policies for
these shops remain **pending** in the repository:
the local run was approved for local evaluation only.

Earlier checks: Zara and Pull&Bear did not yield usable live product data, and
H&M returned HTTP 403. Most large Hungarian shops block or challenge crawlers,
need JavaScript, or opt out of AI crawling; the
[candidate list](docs/hungarian-source-candidates.md) records each one and why.
ABOUT YOU and Answear advertise affiliate feeds, the better route for them:
publisher access, feed samples, and image-use terms are still needed.

A private beta needs two approved sources with at least 100 current offers in
one category, 30 manually checked offers per source, 30 consented shopping
tasks with at least 24 successful, permitted image relevance evidence, and a
live source-to-browser test. The [application progress](docs/application-progress.md)
tracks the implemented work and open gates. The [release handoff](docs/release-gate-handoff.md)
lists the exact outstanding evidence. The [partner access handoff](docs/partner-access-handoff.md)
and [shopper research handoff](docs/shopper-research-handoff.md) provide the
operator steps. The [task evaluation guide](docs/task-evaluation-guide.md)
describes how to record real-user results and replay text/photo rankings.
Private task inputs stay outside Git; the report excludes queries and photos.

## Collection tools and source status

- **Scrapy:** discovery (`collect`) and refresh (`refresh`) with RFC 9309 robots
  handling, Crawl-delay, per-host pacing and stops on 401/403 or a CAPTCHA. Five
  LPP shops (Reserved, Sinsay, Mohito, House, Cropp) are `reviewed` and work; a
  shop that publishes schema.org product data can be added as configuration.
  `benchmark-extraction` classifies a shop's extraction method and timing.
- **Scheduled refresh:** `.github/workflows/fashion-finder-refresh.yml` runs at
  08:17 and 20:17 Budapest time and posts results to the token-protected ingest
  API. It skips until the `FASHION_FINDER_API_URL` and `FASHION_FINDER_API_TOKEN`
  secrets exist. The hosted Render demo now shows the real shops (names, prices,
  links, and photos from the shops' own servers) with word search and **photo
  search**; look search stays off (its text model does not fit in 512 MB). Its
  database and photo index are **temporary** and empty on every wake-up, so
  scheduled runs rediscover the catalogue and then run `index-photos`: that step
  downloads photos politely, encodes them with the pinned INT8 vision model and
  uploads only the vectors, keeping them in an `actions/cache` embedding cache
  (vectors only, never photos). A first run with an empty cache needs about 45
  minutes for ~1,350 photos; later runs rebuild the wiped index from the cache in a
  minute or two. See "Hosted demo" in the [scraper guide](docs/scraper.md). The
  same commands run as a Render cron job or on a self-hosted runner.
- **Playwright:** optional Chromium rendering, used only for shops marked as
  needing JavaScript. It does not guarantee access to a blocked storefront.
- **Crawlee:** compared with Scrapy on a local fixture; it is not required at
  runtime.
- **Feeds:** local CSV imports support all six source keys. ABOUT YOU and
  Answear are feed-only candidates with a dry-run validator and configurable
  local XML-to-CSV conversion; publisher access, the actual field mapping, and
  rights remain pending.

See the [source and licence review](docs/source-review.md),
[live source checks](docs/live-source-status.md), and
[remaining source decisions](docs/remaining-roadmap-decisions.md). The
[data research and plan](docs/data-research/README.md) sizes the working shops,
surveys new Hungarian shops, compares affiliate feed routes and lists licence-safe
test datasets.

## Ingestion pipeline in detail

1. Collect a small batch from one source.
2. Save raw source output to `data/raw/` as JSONL.
3. Normalize to the shared schema.
4. Validate fields and flag missing or malformed data.
5. Deduplicate by source plus source product ID/URL.
6. Upsert offers into local SQLite and append price observations.
7. Export normalized results and a collection-run report.

See the [architecture](docs/architecture.md), [data schema](docs/data-schema.md),
[pipeline roadmap](docs/roadmap.md), and [application implementation plan](docs/application-implementation-plan.md)
for the full contracts and delivery sequence.

## Development stack and checks

- Python 3.12
- Scrapy (robots-aware HTTP collection)
- Playwright for Python (optional Chromium rendering in the collector and diagnostics)
- Pydantic for schema validation
- Django ORM and migrations with SQLite for local persistence, API, and catalogue review
- Next.js and TypeScript for the shopper page
- Optional ONNX Runtime with pinned local Marqo-FashionSigLIP vision and text models
  (INT8 by default), an optional pinned opus-mt-hu-en translator, and a ViT baseline
- Docker Compose, Gunicorn, and WhiteNoise for local deployment
- JSONL for raw snapshots and portable exports
- Pytest, Ruff, ESLint, and TypeScript checks

GitHub Actions runs the Python and web checks and builds both Docker images on
pushes and pull requests. Local Python checks:

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

Existing SQLite catalogues are adopted by `fashion-finder migrate`; see the
[local app setup](docs/app-local-setup.md) before starting a development server.

## Windows quick start

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
# Required for --browser collection and browser diagnostics
python -m playwright install chromium
```

From the repository root, try one bounded Reserved sample:

```powershell
fashion-finder sources list
fashion-finder collect reserved --limit 1
fashion-finder collect all --limit 1
fashion-finder collect reserved --limit 3 --url https://www.reserved.com/hu/hu/noi/ruhak
fashion-finder collect sinsay --limit 300
fashion-finder collect cropp --limit 500 --url https://www.cropp.com/hu/hu/ferfi/ruhazat/farmerek
fashion-finder collect zara --browser --limit 3
fashion-finder collect hm --browser --limit 3
fashion-finder collect pullandbear --browser --limit 3
fashion-finder report <run-id>
fashion-finder export data/exports/catalogue.csv
```

`collect all` runs only the sources marked `reviewed` in `source_definitions.toml` (today the five LPP shops); every other source needs an explicit name. It reports each retailer independently and exits nonzero when any source fails. Use `--url https://...` with a single source to test a specific Hungarian category or product URL. Only HTTPS pages on that retailer's Hungarian storefront are accepted. The CLI never logs in, solves CAPTCHAs, or works around HTTP 403. Requests identify themselves as `FashionFinderBot` with a contact URL (`--contact-url` or `FASHION_FINDER_CONTACT_URL`). Discovery (`collect`) and refresh of known offers (`refresh`) are separate commands; see [the scraper guide](docs/scraper.md).

`--browser` renders both category and product pages in Chromium before the existing
parsers run. Scrapy checks `robots.txt` before each top-level navigation. Its
ten-second request delay applies to those navigations; Chromium also loads page
assets such as scripts and images. The per-source product limit, local storage,
and run reports still apply. The collector waits
1.5 seconds for scripts after each navigation; adjust with `--browser-wait-ms 3000`
(0–10000) if a page renders slowly. Run each blocked source separately to see its
own report. Browser mode has not produced verified records for Zara, H&M, or
Pull&Bear; a browser may still receive HTTP 403 or pages without usable prices.

To check whether a normal Chromium browser can see one category without importing data,
run `python scripts/browser_diagnostic.py zara` after installing Chromium with
`python -m playwright install chromium`. Repeat with `hm` or `pullandbear`.
It reports the HTTP status and visible product link/price counts without importing
any records. A browser can render JavaScript, but it cannot guarantee access to
pages that return HTTP 403. The diagnostic uses no stealth settings.

Reserved, Sinsay, Mohito, House and Cropp run on one LPP storefront platform. Their
category pages embed the listed products with price, regular price, images, colour and
per-size stock, so collection stores offers straight from the listing without one
request per product. Listings are cumulative (`?page=N` repeats earlier pages), so the
collector jumps to the page that should cover `--limit`. On 2026-10-13, 300 Sinsay
jackets took three category requests (pages 1, 5 and 6), not 300 product requests. One product listed under alias URLs
(`coat-2-…`) is stored once, under its plain URL. Pages without the embedded listing
fall back to following product links.

Reserved recognizes the public `kabatok`, `ruhak`, and `puloverek` category paths as coats, dresses, and sweaters. For LPP category URLs, a final path word such as `dzsekik` or `farmerek` maps to its category; a path that names two categories, such as `dzsekik-kabatok`, stays unclassified. The H&M adapter can read complete HUF offers from category cards if the page is accessible, but the live Scrapy check here was blocked before parsing.

Every collection writes raw JSONL under `data/raw/<source>/`, a run report under `data/reports/`, offers and price changes in `data/fashion_finder.sqlite`, and refreshed JSONL/CSV exports under `data/exports/`. These local outputs are ignored by Git. Repeat runs update an offer and add a price observation only when its price or currency changes.

Search the stored HUF offers without making another retailer request:

```powershell
fashion-finder search --query dzseki --max-price 30000 --sort price-asc
fashion-finder search --query "fekete kabát" --sort relevance
fashion-finder search --category dresses --source reserved --sort recent --limit 10
```

Text search ignores accents and translates common clothing and colour words both ways,
so `black coat`, `fekete kabát` and `fekete kabat` find the same listings, Hungarian
or English. It matches title, brand, description, category and colour, word by word:
a word of four or more letters may start or end a longer word (`kabát` finds
`kabátok` and `gyapjúkabát`), while a shorter one must be a whole word, so `red` no
longer matches `Redőzött`. `--sort relevance` (the shopper page's **Best match**)
ranks title hits above category or colour hits, and exact words above partial ones.
The web colour filter works the same way (`blue` matches `sötétkék`), and a size
such as `M` matches a combined size like `M/L`.

Search pages use a keyset cursor: **Load more** continues after the last result
shown, even if that item has since sold out or changed price.

Search returns JSONL with source links, prices, category, image URLs, and last-seen time. The default currency is HUF; use `--currency` for another stored currency. Budget comparisons and price sorting use exact decimal values within one currency. Results reflect the last collection, so unknown stock remains `unknown`.

Known English and Hungarian category labels map to shared keys such as `coats`, `jackets`, `dresses`, `sweaters`, and `shirts`. The original label is kept as `source_category`. Unknown labels stay unclassified and appear in quality reports instead of being guessed. Opening an older SQLite database adds `source_category` and maps its existing category values once.

Check freshness and field quality by retailer and category:

```powershell
fashion-finder quality --stale-after-days 7
fashion-finder quality --output data/reports/quality-check.json
```

The report counts old observations, missing fields, unmapped categories, malformed stored values, and unknown availability. It does not contact retailers or change offer status. An old observation means the listing needs another check; it does not prove the item was removed or sold out. `--output` creates a new JSON file and refuses to overwrite an existing one.

List offers that need a direct check:

```powershell
fashion-finder recheck --source reserved --stale-after-days 7 --limit 20
```

This command is read-only. It includes a direct URL only when the stored URL still passes
the retailer and Hungarian-market checks. Missing items in a bounded sample and HTTP
errors do not mark offers unavailable. See the [source-specific removal policy](docs/remaining-roadmap-decisions.md#removal-and-stock-policy).

To replay a raw file without network access:

```powershell
fashion-finder ingest reserved data/raw/reserved/<run-id>.jsonl
```

If a retailer provides an authorized product export, import a local CSV:

```powershell
fashion-finder import-feed zara path/to/authorized-products.csv
fashion-finder validate-feed aboutyou path/to/mapped-aboutyou.csv
fashion-finder map-xml-feed aboutyou path/to/download.xml path/to/mapping.json path/to/mapped-aboutyou.csv --observed-at <ISO-8601-download-time>
```

The CSV needs `product_url,name,price,currency` headers. Optional headers are
`sku,brand,category,description,original_price,image_url,colour,size,availability`.
Use one product per row, a full HTTPS URL on that retailer's Hungarian storefront,
and `HUF` prices. The command reads a local file; it does not fetch a feed URL or
grant permission to collect retailer content. Valid rows enter the same raw archive,
database, exports, and run report as web collection. Invalid rows are counted and
reported; a partial import exits nonzero. Scheduled feeds and `validate-feed`
also require `observed_at` with a timezone. The dry-run validator writes no
database or raw archive. No partner feed for the candidate sources has been
provided or verified; see the [onboarding steps](docs/hungarian-source-candidates.md).

LPP sources page through a category until `--limit` is met. Other sources sample the first category page only. Neither covers a full catalogue unless you pass its categories with `--url`. The limit is 1–60,000 product offers per source across all starting URLs; `--max-requests` and `--time-budget-minutes` bound the actual work, and each host keeps at least a 10-second delay (longer when its robots.txt asks for it). A JSONL export contains product and image URLs, but this does not grant rights to republish retailer content or images.

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

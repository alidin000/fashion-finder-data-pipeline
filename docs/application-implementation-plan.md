# Fashion Finder: technical application implementation plan

Status: implementation plan, 12 October 2026. This expands the [data pipeline roadmap](roadmap.md) into a Hungary-first shopper application. The roadmap describes ingestion work; this document defines the product, service, and release work that sits on top of it.

Track completed work and release blockers in [application progress](application-progress.md).

## 1. Target outcome and scope

A shopper submits a clothing photo or a text query, corrects the inferred category or colour, sets a budget, and opens a current Hungarian retailer offer. The result page must distinguish a verified identical item from a visually similar alternative. It must show the retailer, HUF price, direct product link, and when the offer was last checked. Show size, stock, shipping, or pickup claims only when the source actually supplies and permits those fields.

Build the first private beta around **one category** (choose women's jackets or trainers from interview evidence) and **two approved Hungarian data sources**. Source approval includes a permitted way to obtain product data and explicit permission for the intended image display and image processing. A successful technical crawl does not establish reuse rights. The [live-source status](live-source-status.md) records 13 Reserved offers, including 10 checked against product pages; it does not establish an approved, production-scale catalogue. Zara, H&M, and Pull&Bear access remains unresolved there.

Out of scope for the first beta: accounts, checkout, in-store inventory, price alerts, price history in the UI, outfit generation, and claims of an exact cross-retailer match without product identity evidence. The pipeline may retain price observations for internal quality work.

The 8–12 week outline below assumes one experienced full-time developer **after source access and image rights are settled**. Retailer agreements may take longer than implementation. Do not treat calendar estimates as release commitments.

## 2. Existing code and intended boundaries

At planning time, the published `main` ref was `60eec9c`. It has the Python ingestion package under `src/fashion_finder_data/`, SQLite tables in `storage.py`, and a local text/price search function in `search.py`. The package targets Python 3.12. Existing offer fields and normalization rules are documented in [data schema](data-schema.md). Confirm the current ref before implementation.

Two later local commits, `5e141af` (collector correctness and CI) and `dc885e0` (optional browser rendering), were on `fix/collector-correctness-ci` when this plan was written. They are **not prerequisites to publishing this document**. The implementing agent should inspect and reconcile them against current `main` before changing overlapping collector code. Do not assume they have been pushed.

```text
Approved retailer feed / bounded collector
                  │
                  ▼
Existing raw JSONL → normalize → quality report → offer store
                                            │
                         ┌──────────────────┴──────────────────┐
                         ▼                                     ▼
                  Search/read API                     Background image indexer
                         │                                     │
                         └────────────┬────────────────────────┘
                                      ▼
                              Next.js shopper UI
```

Keep collection and image indexing out of request handlers. Search must read a previously validated catalogue, so a retailer failure cannot stall a shopper request. Keep raw archives private and outside the public API.

Suggested layout, subject to a small design review when implementation starts:

| Concern | Proposed location | Responsibility |
| --- | --- | --- |
| Ingestion and normalization | `src/fashion_finder_data/` | Extend existing package; preserve CLI and exports. |
| Search service | `src/fashion_finder_data/api/` | FastAPI endpoints and request/response schemas. |
| Browser app | `web/` | Next.js, TypeScript, responsive shopper flow. |
| Catalogue schema evolution | `src/fashion_finder_data/` plus migration directory | Add source policy, variants, and embeddings without rebuilding existing data. |
| Tests | `tests/` and `web/` | API contracts, ranking, UI flows, source policy, and failure handling. |
| Deployment | Choose after local end-to-end proof | Separate web, API, and scheduled worker processes. |

Use SQLite for local development. Move the deployed multi-user beta to PostgreSQL only when deployment/concurrency needs justify it. Add real migrations before such a move; `create_all()` and ad hoc SQLite alterations are insufficient for a production schema history. Start image search with an in-process scan of a small, versioned embedding set. Introduce a vector index such as pgvector only after measured query latency or catalogue size requires it.

## 3. Work packages in dependency order

### A. Validate the shopper task and source gate

**Deliverables**

1. Interview 8–12 Hungary-based shoppers about a recent attempt to find a similar garment. Record the original photo/query, useful attributes, price range, and why a result was or was not useful. Inspect two or three competing flows with the same tasks.
2. Select one launch category and write 30 real, consented evaluation tasks. Store only the labels and images the team has permission to retain. Record expected category, relevant results, and acceptable alternatives.
3. Create a source manifest for each candidate retailer or feed. Record market, access method and terms, allowed image display, allowed image processing/retention, attribution, deep-link rules, refresh interval, available fields, rate limits, and contact/approval evidence. Unknown permission means that use remains blocked.
4. Secure two approved sources with at least 100 current, usable offers each in the chosen category. Manual review must confirm that URLs resolve to the intended Hungarian products.

**Acceptance:** a reviewer can trace every beta offer and image to an approved source policy; two sources meet the 100-offer threshold; the 30 tasks and relevance rubric are written before model tuning. If image rights lag, continue a private text-only pilot and mark photo search blocked.

### B. Harden the collection foundation

Reconcile the two local candidate commits with current `main` if available. Preserve a strict total item limit per retailer run, Decimal-safe JSON price parsing, actionable extraction errors, and automated `pytest`/`ruff` checks. These are known candidates for regression, not a substitute for testing against approved live sources.

Extend ingestion with a source-specific policy gate and freshness rule. Do not publish an offer if its source policy does not allow that use. A failed request, 403, or missing page must create a review/recheck signal rather than silently marking a product out of stock. Keep the existing idempotent retailer plus product-ID/URL upsert behavior and price observations. Schedule refreshes outside user requests; persist run status, rejection reasons, and last successful source check.

**Acceptance:** repeat feed/collector runs do not duplicate offers; an inaccessible source leaves existing stock claims unchanged; a prohibited image never reaches a public response or the embedding queue; run reports explain source failures. Test these contracts with local fixtures and perform a bounded live check against each approved source before beta.

### C. Add a read-only catalogue API

Implement FastAPI with versioned routes. Prefer shared Pydantic models for validation and OpenAPI output. Reuse existing normalization and search behavior where it fits; avoid importing CLI code into request handlers. The API reads one configured database URL/path and never starts a crawl.

| Route | Request | Minimum response/behavior |
| --- | --- | --- |
| `GET /api/v1/health` | None | Service status and catalogue timestamp; no secrets. |
| `GET /api/v1/search` | `q`, `category`, `colour`, `size`, `max_price`, `source`, `sort`, `limit`, `cursor` | Bounded, stable page of eligible offers plus next cursor. Currency is HUF for the first market. |
| `POST /api/v1/search/image` | Multipart image plus the same filters | Extracted editable attributes and ranked eligible offers. Reject invalid media, excess bytes, and decoding failures. |
| `GET /api/v1/offers/{id}` | Stable internal offer ID | Current permitted public fields or 404 if hidden/stale. |

The public offer object should contain `id`, `source_key`, `source_product_id` when available, `title`, normalized `category`, `price_amount` as a decimal **string**, `currency`, `source_url`, permitted `image_urls`, `availability_status`, `last_checked_at`, and `match_type`. Add `size_options`, `colour_names`, shipping, or pickup only when verified from the source. Do not expose raw product JSON, local file paths, source credentials, or internal policy evidence. `match_type` values are `same_item`, `close_alternative`, and `style_inspiration`; default to an alternative label until identity is verified. Return structured validation errors and clear empty results.

Define stable sort keys and cursor semantics before pagination. Bound `limit` (for example 1–50), reject invalid price and currency values, and avoid unrestricted SQL filtering. Set a deployment-specific CORS allowlist; no wildcard origin with credentials. If image search is disabled by policy, return a clear capability status rather than a fabricated match.

**Acceptance:** API integration tests cover prices and budgets, pagination without duplicates, unknown stock, stale/prohibited offers, malformed uploads, no-result responses, and source outage isolation. OpenAPI examples match actual responses. A local end-to-end call returns an approved stored offer.

### D. Extend the catalogue without losing provenance

Add schema and migrations incrementally:

| Entity/field | Required contract |
| --- | --- |
| `source_policy` or checked-in source manifest | Source and market; allowed display/indexing uses; attribution; refresh SLA; policy version/evidence reference. Keep sensitive agreements private. |
| `retailer_offer` additions | Stable internal ID, last successful check, visibility/freshness state, policy version used at publish time. Preserve source identity and original URL. |
| `offer_variant` | Source-supplied size/colour and availability where available; unknown otherwise. Avoid implying that a general size list proves stock. |
| `offer_embedding` | Offer/image ID, model and preprocessing version, vector, computed time, image-rights provenance; replace or invalidate on policy/image change. |
| `identity_match` (only if needed) | Two offer IDs, evidence type, verification time. Visual similarity alone cannot populate it. |

Define source-specific stale thresholds. Hide an offer, or label it visibly stale, after that threshold; choose one rule per beta source and document it. Keep prices as exact decimal values through storage and API serialization. Record migrations and backfill steps for existing SQLite files; test upgrade from a copy of the prior schema.

**Acceptance:** an existing local catalogue upgrades without loss; stale status is computed from source policy and last successful check; changing image permission removes affected images from responses and the index.

### E. Build the shopper interface

Implement a responsive Next.js/TypeScript app with one search/results route. Provide photo upload with a **real local preview**, text entry, editable category/colour, explicit HUF budget, size and retailer filters, stable sort, loading/empty/error states, clear retailer deep links, and a last-checked label. The UI must use API results; demo fixture cards cannot silently appear in live mode. No account is required for the private beta.

The existing clickable prototype is useful as a flow reference, not production code. In the reviewed prototype, entering `black jacket under 15000 Ft` still displayed a 27,995 Ft jacket because text search did not parse the budget; upload only showed a filename; a 390 px viewport overflowed to 419 px. The implementation must handle these cases with an explicit budget control (and optional query parsing), an image preview, and mobile layout checks.

**Acceptance:** at 390 px and desktop widths, no horizontal overflow; the 15,000 Ft budget excludes higher-priced items; upload preview and correction controls work; each displayed card has a valid source label, price/freshness state, match label, and direct link. Test one real browser flow against the local API.

### F. Add visual matching behind an evaluation gate

Use a pretrained image encoder on **permitted** product images and uploaded queries. Keep model name, preprocessing version, and embedding version together so query and catalogue vectors are compatible. Filter by category, price, source, and verified size before ranking where possible. Combine text/attribute relevance with image similarity; tune weights on the 30 labeled tasks rather than intuition. Do not infer `same_item` from vector distance. A visually close coat may be a different product.

Validate image file type by decoding, cap bytes and dimensions, strip metadata, and process uploads ephemerally. Do not log raw images or retain them by default. Avoid fetching arbitrary user-supplied URLs from the API. Protect request time and memory with bounded processing; return a useful text/filter fallback when image processing fails. Track top-5 relevance, time to first result, and false exact-match labels.

**Acceptance:** all indexed images pass the source policy gate; the 30-task evaluation report compares text-only and image-assisted ranking using the same catalogue; the chosen ranking improves useful top-5 results without unacceptable latency; no exact claim is generated from similarity alone. If the report does not support release, keep text search available and iterate on labels/catalogue quality.

### G. Instrument, operate, and run a private beta

Collect minimal anonymous events: search type, filter use, result count, result click, source, and error code. Exclude raw images, full free-text queries, and persistent personal identifiers unless a specific consented research process needs them. Set event retention and deletion rules before deployment. Monitor ingestion success, offer freshness, broken links, API errors/latency, image-index lag, and source-specific spend. Keep source secrets in deployment secret storage.

Deploy web, API, and scheduled worker separately. Add CI for Python tests/lint and web lint/typecheck/tests; build both deployable artifacts. Provide documented environment variables, health checks, backup/restore, migration, and rollback steps. Run a live smoke test from the deployed environment for an approved source import, one API query, one browser upload/query, and one retailer deep link. Record where commands ran and the observed output. Do not call the beta ready on mocked checks alone.

Invite 10–20 people to the private beta. Measure task completion, useful first-page results, retailer clicks, broken links, stale prices, repeat use, and cost per usable offer. Ask participants whether match labels and freshness are understandable. Review errors weekly and fix catalogue quality before adding retailers or features.

**Acceptance:** the release gate below passes; a rollback path is documented and rehearsed; the team has a named owner for source failures and user reports.

## 4. Private beta release gate

All items are required unless an explicit product decision changes the scope:

- Two approved sources, each with at least 100 current offers in the launch category and permission for every image use in the beta.
- Manual audit of 30 offers **per source** for current HUF price, correct category, working Hungarian deep link, and truthful availability/size presentation.
- Thirty real shopping tasks evaluated before launch. At least 24 must yield three useful current options across the two shops; report the denominator and any tasks that lack enough catalogue coverage. Evaluate photo top-5 separately and disclose its measured result rather than assuming it passes.
- No fabricated stock, delivery, exact-match, or image-rights claims. Stale offers follow each source's documented policy.
- Budget filter and uploaded-image preview work in a mobile browser without horizontal overflow. Invalid image files are rejected and uploaded bytes are deleted after processing by default.
- CI passes, migrations work from the previous local schema, and the deployed live smoke test covers source → catalogue → API → browser → retailer link.

These are initial decision thresholds, not claims that the present catalogue meets them. If two-source access is blocked, release an explicitly labeled text-only/private research prototype rather than a cross-retailer shopping beta.

## 5. Delivery sequence and agent handoff

| Sequence | Work | Dependency | Reviewable output |
| --- | --- | --- | --- |
| 0, week 1 | Interviews, category choice, 30-task set | Participant consent | Short research note and evaluation set. |
| 1, parallel | Source policies and two approved feeds/collectors | Retailer/partner access | Policy manifests, sample imports, 30-offer audits. |
| 2, weeks 2–6 | Collection hardening, schema migration, read API | Current repo and one approved source | Tests, OpenAPI, local end-to-end call. |
| 3, weeks 3–6 | Responsive UI and text/budget flow | API contract | Browser flow and mobile evidence. |
| 4, weeks 4–8 | Permitted image indexing and ranking evaluation | Image rights and labeled tasks | Reproducible 30-task report. |
| 5, weeks 8–12 | Deployment, live smoke, 10–20 user beta | Release gate | Runbook, measurements, prioritized fixes. |

### Instructions for an implementing agent

1. Read `README.md`, [architecture](architecture.md), [pipeline roadmap](roadmap.md), [data schema](data-schema.md), and [live-source status](live-source-status.md). Fetch `main` and inspect its actual files and tests. Do not assume this plan or the local candidate commits describe the latest code.
2. Take **one work package at a time**. Start with the source-policy/data-contract work and the existing collector correctness/CI reconciliation. State any blocked external access as a dependency; continue independent API/UI work with clearly marked synthetic fixtures.
3. Before coding each package, write the exact schema/API change and its tests in the pull request description or task notes. Preserve backward compatibility for the existing CLI, SQLite catalogue, and JSONL/CSV export unless a migration explicitly replaces it.
4. Implement behavior-focused tests for policy gating, price precision, freshness, pagination, uploads, ranking labels, and source failure handling where those behaviors change. Run `python -m pytest` and `ruff check .` for Python changes; run the web project's lint, typecheck, and tests for web changes. Record the exact commands and results.
5. Run a live end-to-end smoke test for any changed external integration or shopper flow. If access or rights prevent it, mark the work unverified and provide copy-paste instructions for the person who can run it. Do not claim a source works because a fixture passed.
6. Keep changes small and reviewable. Update this plan's status and the source-status document when evidence changes. Do not mark checklist gates complete from assumptions or from another market's website.

After the private beta, decide whether to add categories/retailers, alerts, price history, outfit matching, delivery totals, or store stock using measured shopper demand, source field coverage, and operating cost. Each new retailer still needs its own approved data and image policy.

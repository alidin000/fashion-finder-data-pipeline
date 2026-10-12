# Application implementation status

Updated 12 October 2026. This tracks the [technical application plan](application-implementation-plan.md). It describes the implementation in this revision, not a public release. Check the Git ref before using it as evidence for another branch.

| Work package | Status | Evidence and next action |
| --- | --- | --- |
| Shopper interviews and category choice | Blocked on research participants | No 8–12 interviews or consented 30-task evaluation set exists. Choose the launch category from that evidence. |
| Approved Hungarian sources | Blocked on retailer/partner access and rights | No two approved sources or image display/processing permissions have been supplied. The example policy denies all use. Existing Reserved collection evidence is extraction evidence only. |
| Collector correctness and CI | Included in this revision | Commits `5e141af` and `dc885e0` include limit/price/error fixes, CI, and optional browser diagnostics. Live Zara/H&M/Pull&Bear extraction remains unverified. |
| Public catalogue boundary | Implemented | A policy manifest grants listing/image uses separately; absent approval denies results. The API hides stale, out-of-stock, and foreign-host links. It does not mutate collector data or infer removal from HTTP errors. |
| Read-only API | Implemented for the bounded catalogue | FastAPI routes: health, search, offer detail, image-search capability response. Search supports HUF budget, category, colour, size, source, sort, and cursor pages. The image route validates uploads and returns 503 because no permitted index exists. Search currently scans rows in memory; benchmark and move filtering to SQL before scale. |
| Shopper UI | Implemented | Next.js page with responsive search, explicit and phrase-derived HUF budget, filters, match labels, source links, freshness, local photo preview, and empty/error states. It never inserts demo offers into API results. |
| Schema migrations, variants, embeddings, and identity matches | Pending | Existing offer fields support the first text flow. Add versioned migrations and richer models when approved variant/image data is available. Do not claim cross-retailer exact matches from similarity. |
| Visual relevance evaluation | Blocked on image rights and tasks | No pretrained model/index or 30-task report exists. The product displays its unavailable capability clearly. |
| Deployment, anonymous analytics, and private beta | Pending | No public deployment or beta users. Add retention rules, operational monitoring, migrations/rollback, live approved-source smoke test, and release-gate evidence before beta. |

## Verification performed locally

- Python: `.venv/bin/python -m pytest` — 37 tests passed; `.venv/bin/python -m ruff check .` and `ruff format --check .` passed after formatting.
- Web: `npm run typecheck`, `npm run lint`, `npm run test` (two query cases), and an optimized Next.js build passed with Node 24.15.0. Build needed execution outside the restricted sandbox because its child TypeScript process returned no output there.
- Synthetic HTTP/browser smoke: Uvicorn on local loopback port 8765 and Next dev on 3100. One fake 9,995 Ft jacket and one fake 27,995 Ft jacket were inserted into a temporary SQLite file with a **local-only synthetic policy**. `GET /api/v1/search?max_price=15000` returned only the cheaper item. Chromium at 390 px entered `black jacket under 15000 Ft`, showed only that item, filled the budget control with `15000`, displayed the local photo preview, and had zero horizontal overflow. A 1440 px viewport also had zero overflow. A final 390 px pass confirmed that the unavailable image capability shows a clear message and does not upload the selected photo. No retailer source or deep link was live-tested by this fixture.
- Dependency review: `npm audit --omit=dev` found zero production dependency vulnerabilities. Full `npm audit` reported five high advisories in the development-only `eslint-config-next` → `fast-glob` → `micromatch` → `braces` chain; npm offered only a breaking downgrade to Next 14 tooling. Recheck when upstream publishes a compatible fix.

**Release state:** not beta-ready. The two-source rights gate, 30-offer audits per source, 30 real shopping tasks, image relevance evaluation, deployment, and deployed live smoke test remain open. Do not substitute the synthetic local smoke test for any of them.

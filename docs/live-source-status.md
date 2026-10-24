# Hungarian source status

Checked 2026-10-12 from the local development environment. These are access and extraction observations, not permission to reuse retailer content.

## 2026-10-24: new collector, small live smoke test

Run from the local development network with the new collector (`FashionFinderBot/0.1.0`,
contact URL = repository), on three sources the repo already treats as reviewed. Each
host saw at most 17 requests in total, at least 10 s apart (robots.txt `Crawl-delay: 10`
on Reserved and Mohito; the 10 s default on Sinsay, which sets none). No 403, 429 or
challenge page appeared. The scraped data stayed in a scratch directory outside Git.

| Step (UTC) | Command | Reserved | Sinsay | Mohito |
|---|---|---|---|---|
| Discovery, 19:18 | `collect <shop> --limit 30 --max-requests 6` | 4 requests (robots 200, tdmrep 301, category 301 then 200); 30 valid, 0 rejected | 3 requests (tdmrep 404); 30 valid, 0 rejected | 3 requests (tdmrep 301); 30 valid, 0 rejected |
| Benchmark, 19:20 | `benchmark-extraction --per-source 8 --max-requests 12` | JSON-LD on 8/8 product pages; price 8/8, sizes 0/8; p50/p95 655/828 ms | JSON-LD 8/8; price 8/8, sizes 0/8; 792/967 ms | JSON-LD 8/8; price 8/8, sizes 0/8; 482/665 ms |
| Refresh, 19:22 | `refresh --limit 90 --max-requests 6` (all 90 forced due) | 3 requests (robots, page 1, one jump); 30 due checked: 30 unchanged (no write); whole category read: 425 new | 30 unchanged; 456 new | 30 unchanged; 185 new |

- **Field coverage** of the 1,156 stored offers after the refresh (`fashion-finder
  quality`): price, currency, image, brand, colour, size, SKU, known availability and
  mapped category 100 % for all three; regular (struck-through) price 14.3 % Reserved,
  25.9 % Sinsay, 38.1 % Mohito; description 0 % (the listing has none). Availability:
  1,155 in stock, 1 out of stock.
- **TDMRep:** `/.well-known/tdmrep.json` answered 301 without a target on Reserved and
  Mohito and 404 on Sinsay: no reservation. The text collector checks it once per host
  and caches it for 7 days.
- **Product pages vs listings:** LPP product pages carry JSON-LD with price but no sizes;
  the category listing's embedded JSON has sizes and per-size stock. LPP shops therefore
  refresh through their listings (`refresh = { strategy = "listing" }`): 3 requests
  re-observed 1,156 offers. JavaScript was not needed anywhere.
- **Cycle estimate** (listing strategy, 10 s spacing): the 28/29/14 curated categories
  of Reserved/Sinsay/Mohito need 56/58/28 requests, under 0.2 h per shop per cycle;
  one connection per shop is enough for a 12-hour interval.
- **Robots-only checks** for the new candidates, 18:19 UTC, one request each:
  sportano.hu allows `*` except `/guestwish`, `/rma/guest/login/`, `/catalog/*`, two
  static paths and `/blog/wp-admin/`, and lists `https://sportano.hu/sitemap/sitemap_hu.xml`; ecipo.hu and modivo.hu allow `/p/` and
  plain `/c/` category paths and list no sitemap. No product page of these shops was
  fetched; they stay unverified candidates.

## Earlier checks (2026-10-12/13)


An additional 2026-10-12 diagnostic from this environment returned HTTP 403 for
each Zara, H&M and Pull&Bear starting category, both via ordinary `curl` and via
standard headless Chromium (Playwright). Their `robots.txt` URLs also returned
HTTP 403 to `curl`. This confirms an environment-specific access failure; it does
not prove the same result from every network or identify the retailer's exact reason.

On 2026-10-13 the LPP listing adapter collected from five storefronts in this
environment. Every `robots.txt` and category page returned HTTP 200. Each run used
3–4 requests and had zero rejected records, with 100% price and image completeness.
Reserved, Sinsay and Mohito list coats or jackets; House and Cropp list mixed outerwear,
so their default category stays unclassified. One random stored offer per source
matched its live product page on SKU and HUF price. A repeat Reserved and House run
deduplicated all 440 offers.

| Source | Public starting page | Live result | Next action |
|---|---|---|---|
| Reserved | [HU coats](https://www.reserved.com/hu/hu/noi/kabatok), [HU dresses](https://www.reserved.com/hu/hu/noi/ruhak) | Success, 2026-10-13: 300 coats from the embedded listing. Earlier product-page runs: ten coats and three dresses. | Add more categories with `--url`. |
| Sinsay | [HU jackets](https://www.sinsay.com/hu/hu/noi/ruhazat/dzsekik) | Success, 2026-10-13: 300 of 504 listed jackets, from pages 1, 5 and 6. | Add more categories with `--url`. |
| Mohito | [HU coats](https://www.mohito.com/hu/hu/ruhazat/kabatok) | Success, 2026-10-13: the whole category, 206 distinct products under 217 listing entries. | Add more categories with `--url`. |
| House | [HU coats and jackets](https://www.housebrand.com/hu/hu/noi/ruhak/kabatok-dzsekik) | Success, 2026-10-13: the whole category, 140 distinct products under 185 listing entries. | Add more categories with `--url`. |
| Cropp | [HU jackets and coats](https://www.cropp.com/hu/hu/noi/ruhazat/dzsekik-kabatok) | Success, 2026-10-13: the whole category, 132 products. | Add more categories with `--url`. |
| Zara | [HU jackets](https://www.zara.com/hu/en/woman-jackets-l1114.html) | Product link found, but the product response contained no usable Product metadata or price. Standard Playwright Chromium also received HTTP 403. | Obtain an allowed feed or another documented source; do not bypass the block. |
| H&M | [HU jackets](https://www2.hm.com/hu_hu/noi/vasarlas-termek-szerint/dzsekik/dzsekik.html) | Live Scrapy check: HTTP 403 for `robots.txt` and the category; no records. A separate ordinary HTTP diagnostic received the public category page with product cards, but its individual product page returned 403. Category-card parsing is implemented and fixture-tested, not live verified in Scrapy. | Obtain an allowed feed or a documented access method; rerun a bounded live check from the deployment environment. |
| Pull&Bear | [HU shirts](https://www.pullandbear.com/hu/noi/ruhazat/bluzok-es-ingek-n6525) | Public category response yielded no product links; standard Playwright Chromium received HTTP 403. | Obtain an allowed feed or another documented source; do not bypass the block. |

The CLI continues other sources when one fails. Each source gets its own run report. The collector uses Scrapy HTTP requests by default and can render pages with Chromium using `collect <source> --browser`. Scrapy checks `robots.txt` and applies a ten-second delay to top-level navigation in both modes; Chromium also loads page assets. On 2026-10-12, one live Reserved browser collection succeeded with one discovered, parsed, and valid HUF offer, and zero errors. Browser collection of Zara, H&M, and Pull&Bear remains unverified.

LPP sources page through a category until the limit is met and take stock from the
listing's per-size flags. Other sources sample the first category page and product
pages only. No source downloads photos or covers every category by default. Of eight
configured web adapters, five are verified live collectors.

An `import-feed` command can ingest a local, authorized product CSV for any configured source key. ABOUT YOU and Answear are feed-only candidates, with no supplied partner feed or permission. The command has no feed credentials or live feed integration. A successful feed import does not establish that the corresponding web collector works.

The read-only `recheck` command now lists stale offers for direct review. The [source-specific removal policy](remaining-roadmap-decisions.md#removal-and-stock-policy) does not turn missing sample results or blocked requests into unavailable stock.

On 2026-10-12, ten stored Reserved offers were cross-checked against fresh public product-page responses: seven coats (`144KY-74X`, `144KY-99X`, `050HW-99X`, `038HW-99X`, `158KY-02M`, `158KY-99X`, `966HU-02M`) and three dresses (`504KE-83X`, `519KE-83X`, `519KE-99X`). All ten returned HTTP 200 and matched their stored product path, SKU, title, HUF currency, and price in page metadata. This checks the sampled listings at that time; it does not confirm stock or image reuse rights.

A local quality report over those 13 live-collected offers found zero stale records, missing prices, missing images, or malformed URLs. All 13 still have unknown availability, as the pages did not provide a verified stock state.

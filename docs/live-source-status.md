# Hungarian source status

Checked 2026-10-12 from the local development environment. These are access and extraction observations, not permission to reuse retailer content.

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

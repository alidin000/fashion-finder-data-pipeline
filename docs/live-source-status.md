# Hungarian source status

Checked 2026-10-12 from the local development environment. These are access and extraction observations, not permission to reuse retailer content.

| Source | Public starting page | Live result | Next action |
|---|---|---|---|
| Reserved | [HU coats](https://www.reserved.com/hu/hu/noi/kabatok), [HU dresses](https://www.reserved.com/hu/hu/noi/ruhak) | Success: ten coats and three dresses, all with HUF prices, raw records, SQLite offers and exports. An earlier repeat run deduplicated one offer. | Manually cross-check a broader sample and verify more categories before widening coverage. |
| Zara | [HU jackets](https://www.zara.com/hu/en/woman-jackets-l1114.html) | Product link found, but the product response contained no usable Product metadata or price. Standard Playwright Chromium also received HTTP 403. | Obtain an allowed feed or another documented source; do not bypass the block. |
| H&M | [HU jackets](https://www2.hm.com/hu_hu/noi/vasarlas-termek-szerint/dzsekik/dzsekik.html) | Live Scrapy check: HTTP 403 for `robots.txt` and the category; no records. A separate ordinary HTTP diagnostic received the public category page with product cards, but its individual product page returned 403. Category-card parsing is implemented and fixture-tested, not live verified in Scrapy. | Obtain an allowed feed or a documented access method; rerun a bounded live check from the deployment environment. |
| Pull&Bear | [HU shirts](https://www.pullandbear.com/hu/noi/ruhazat/bluzok-es-ingek-n6525) | Public category response yielded no product links; standard Playwright Chromium received HTTP 403. | Obtain an allowed feed or another documented source; do not bypass the block. |

The CLI continues other sources when one fails. Each source gets its own run report. The browser and ordinary HTTP checks were diagnostic only; the collector uses Scrapy HTTP requests, follows `robots.txt`, and waits ten seconds between requests to a domain.

The current collector samples the first category page and product pages only. It does not paginate, infer unavailable stock, download photos, or cover every product. Four configured adapters are not four verified live collectors.

An `import-feed` command can ingest a local, authorized product CSV for any of the four sources. It has no feed credentials or live feed integration. A successful feed import does not establish that the corresponding web collector works.

The read-only `recheck` command now lists stale offers for direct review. The [source-specific removal policy](remaining-roadmap-decisions.md#removal-and-stock-policy) does not turn missing sample results or blocked requests into unavailable stock.

On 2026-10-12, ten stored Reserved offers were cross-checked against fresh public product-page responses: seven coats (`144KY-74X`, `144KY-99X`, `050HW-99X`, `038HW-99X`, `158KY-02M`, `158KY-99X`, `966HU-02M`) and three dresses (`504KE-83X`, `519KE-83X`, `519KE-99X`). All ten returned HTTP 200 and matched their stored product path, SKU, title, HUF currency, and price in page metadata. This checks the sampled listings at that time; it does not confirm stock or image reuse rights.

A local quality report over those 13 live-collected offers found zero stale records, missing prices, missing images, or malformed URLs. All 13 still have unknown availability, as the pages did not provide a verified stock state.

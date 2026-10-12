# Hungarian source status

Checked 2026-10-12 from the local development environment with a one-product limit. These are access and extraction observations, not permission to reuse retailer content.

| Source | Public starting page | Live result | Next action |
|---|---|---|---|
| Reserved | [HU coats](https://www.reserved.com/hu/hu/noi/ruhazat/kabatok) | Success: one product, HUF price, raw record, SQLite offer and exports. Repeat run deduplicated the offer. | Verify more categories and a manual sample before widening coverage. |
| Zara | [HU jackets](https://www.zara.com/hu/en/woman-jackets-l1114.html) | Product link found, but the product response contained no usable Product metadata or price. Standard Playwright Chromium also received HTTP 403. | Obtain an allowed feed or another documented source; do not bypass the block. |
| H&M | [HU jackets](https://www2.hm.com/hu_hu/noi/vasarlas-termek-szerint/dzsekik/dzsekik.html) | HTTP 403 from the category request. | Obtain an allowed feed or access method. |
| Pull&Bear | [HU shirts](https://www.pullandbear.com/hu/noi/ruhazat/bluzok-es-ingek-n6525) | Public category response yielded no product links; standard Playwright Chromium received HTTP 403. | Obtain an allowed feed or another documented source; do not bypass the block. |

The CLI continues other sources when one fails. Each source gets its own run report. The browser check was diagnostic only; the collector uses Scrapy HTTP requests, follows `robots.txt`, and waits ten seconds between requests to a domain.

The current collector samples the first category page and product pages only. It does not paginate, infer unavailable stock, download photos, or cover every product. Four configured adapters are not four verified live collectors.

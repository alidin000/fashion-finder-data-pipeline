# Remaining roadmap decisions

Checked 2026-10-12. This page records decisions and the evidence needed to reopen them.

## Source access and Pull&Bear

Zara, H&M and Pull&Bear web collection is not verified. The latest bounded results are in
[live source status](live-source-status.md). No authorized feed or access instructions have
been supplied. `import-feed` is ready for a local retailer-provided CSV, but a successful
import is not proof that the web adapter works. Source-specific live extraction must be
verified against at least ten current Hungarian products per source, including price,
currency, URL and category, before labeling an adapter live.

The Pull&Bear web extractor remains a clean-room implementation. It uses no code from the
unlicensed candidate repository. Further extraction work needs a usable, allowed source
that provides product fields; an HTTP 200 product shell with no data cannot be parsed
into an offer. No access-control workaround is part of the plan.

## Google Shopping discovery

Decision: do not add a Google Shopping scraper. Search results would be discovery
candidates, not authoritative offers or a complete retailer catalogue. The official
[Merchant Products API](https://developers.google.com/merchant/api/guides/products/overview)
manages products in a Merchant Center account; it does not grant access to all retailers'
inventories. The official
[Custom Search JSON API](https://developers.google.com/custom-search/v1/introduction)
returns search results for a configured search engine and needs an API key and engine ID.
Neither gives this project a verified retailer feed. Revisit an optional discovery adapter
only with an approved source, measured HU retailer coverage, and a direct-source check
before importing any candidate as an offer.

## Scrapy versus Crawlee

`scripts/benchmark_crawlers.py` runs both frameworks against one local HTTP fixture:
a category with four links, three complete products and one incomplete product. Both
frameworks honor the fixture's robots.txt and use one worker. On this machine, Scrapy
2.19.0 extracted three offers and rejected one incomplete page in 0.392s; Crawlee 1.10.4
did the same in 0.673s. These are one-run startup-inclusive timings, not a statistical
speed claim. Run it again with
`pip install -e ".[crawlee-benchmark]"` and
`python scripts/benchmark_crawlers.py`. The script checks both outputs match.

Maintainability: both can use CSS selectors and request queues; Crawlee's Parsel crawler
could reuse extraction concepts, but replacing the working Scrapy spider would require
rewriting scheduling, robots handling, per-source reports and failure boundaries. The
fixture does not reproduce live blocks or JS rendering. Keep Scrapy as the default until
a real allowed source shows a measurable Crawlee extraction or operations advantage.

## Removal and stock policy

`fashion-finder recheck` reports stale offers with direct product URLs where safe. It
never changes stock or listing state. `status=active` means the offer row is retained,
not that stock is verified. The per-source rules are in `removal.py` and returned in
the command's JSON:

| Source | Evidence accepted for stock | Missing from bounded sample | HTTP 403/429/5xx | HTTP 404/410 |
|---|---|---|---|---|
| Reserved | Explicit Product offer availability on an accessible product page | Recheck | Recheck | Manual review |
| H&M | None verified from category cards | Recheck | Recheck | Manual review |
| Zara | None verified | Recheck | Recheck | Manual review |
| Pull&Bear | None verified | Recheck | Recheck | Manual review |

For any source, an authorized feed's explicit availability can set stock state for that
observed row. Feed omission cannot. A blocked page, parsing failure, stale observation,
or an absent item in a partial category crawl cannot establish unavailability. A direct
404/410 is a review signal until that source's behavior is verified; no automatic
unavailable transition or deletion is enabled.

## Image embeddings

Decision: defer. The verified live catalogue has 13 offers from one retailer, so an
image similarity result would be dominated by one source and a tiny sample. Before an
experiment, obtain image processing rights, verify at least two retailers with at least
100 current offers each, and create a labeled evaluation set of at least 30 fashion
queries with relevant and irrelevant matches. Measure retrieval quality and cost against
the text search baseline. Product image URLs alone do not establish image processing
rights.

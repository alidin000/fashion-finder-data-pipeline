# Hungarian source candidates for the first private beta

Checked 13 October 2026. Working category: women's jackets, pending shopper
interviews. These are
**candidates**, not approved sources or evidence that their current feeds meet
the 100-offer threshold.

| Source | Public feed evidence | Access path |
| --- | --- | --- |
| ABOUT YOU Hungary | [VIVnetworks program](https://www.vivnetworks.com/hu/affiliate-katalog/aboutyouhu/) advertises an XML product feed. | Join the ABOUT YOU program through a CJ publisher account. |
| Answear Hungary | [VIVnetworks program](https://www.vivnetworks.com/hu/affiliate-katalog/answearhu/) advertises an XML product feed. | Join the Answear program through a CJ publisher account. |

VIVnetworks' [CJ feed guide](https://www.vivnetworks.com/en/product-feeds-on-cj-com/)
describes creating a product export under **Account > Subscriptions**. It says
CJ HTTP/S feed downloads need account credentials and that the selected website
determines the affiliate links. A public program listing therefore does not
provide a usable feed URL or establish permission to display or process images.

## What is ready locally

The `aboutyou` and `answear` source keys accept canonical Hungarian product URLs
under `https://aboutyou.hu/p/`, `https://www.aboutyou.hu/p/`,
`https://answear.hu/p/`, and `https://www.answear.hu/p/`. They are **feed-only**:
`collect` and browser diagnostics do not crawl them. The scheduled worker can
watch `data/feeds/aboutyou.csv` and `data/feeds/answear.csv`, but it blocks each
file until its source policy is approved with `access_method: "feed-csv"`.
There is no CJ downloader or XML-to-CSV mapping because no account access or
sample feed has been supplied.

After obtaining a permitted feed and mapping it to the canonical CSV, check it
without writing data:

```bash
fashion-finder validate-feed aboutyou path/to/aboutyou.csv
fashion-finder validate-feed answear path/to/answear.csv
```

The required columns are `product_url,name,price,currency,observed_at`; the
timestamp must include a timezone and cannot be in the future. Prices must be
valid HUF values. Optional columns are `sku,brand,category,description,
original_price,image_url,colour,size,availability`. The validator reports up
to 20 row errors and exits nonzero if any row fails or the file has no products.
It checks structure and source URLs; it does **not** verify publisher rights,
feed freshness, product availability, or the source's claim that an image may be
used. It does not create a database, archive, or source approval.

## When access is granted

1. Keep the advertiser approval and full program terms in a private evidence
   store. Record the allowed website, attribution, link rules, feed refresh
   interval, and separate image display, processing, and retention permissions.
2. Download a small feed sample using the publisher account. Keep credentials,
   feed URLs, and raw exports outside Git.
3. Inspect its actual fields, currency, market URLs, image links, availability,
   and categories. Implement and test a mapping to the pipeline's timestamped
   CSV format. Use the feed's observation time only if it is supplied; otherwise
   record the download time as the observation time. `product_url` must be the
   direct Hungarian product page; keep any permitted affiliate tracking link
   separate until the offer model and link rules are reviewed.
4. Run `validate-feed` against the mapped CSV and manually review a sample of
   rows against the partner terms. Keep policies pending until the intended
   listing and image uses are confirmed. Then record the permission evidence
   privately, set the approved policy, and deliver the CSV atomically to
   `data/feeds/<source>.csv` for the worker.
5. Check at least 100 current public-eligible jackets per approved source and
   manually audit 30 offers per source. Run a live import-to-browser smoke test
   and open a real Hungarian product link from the deployed environment.

Thirty consented shopping tasks must still come from real participants. Public
product listings or fashion datasets cannot stand in for participant consent,
usefulness labels, and task-completion evidence. The [evaluation guide](task-evaluation-guide.md)
describes the private, pseudonymous report format.

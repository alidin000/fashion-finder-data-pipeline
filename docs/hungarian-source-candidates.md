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

## When access is granted

1. Keep the advertiser approval and full program terms in a private evidence
   store. Record the allowed website, attribution, link rules, feed refresh
   interval, and separate image display, processing, and retention permissions.
2. Download a small feed sample using the publisher account. Keep credentials,
   feed URLs, and raw exports outside Git.
3. Inspect its actual fields, currency, market URLs, image links, availability,
   and categories. Implement and test a mapping to the pipeline's timestamped
   CSV format. Use the feed's observation time only if it is supplied; otherwise
   record the download time as the observation time.
4. Add source keys and Hungarian URL validation for these **feed-only** sources.
   Do not invent a storefront crawler. Keep their source policies pending until
   the intended listing and image uses are confirmed.
5. Check at least 100 current public-eligible jackets per approved source and
   manually audit 30 offers per source. Run a live import-to-browser smoke test
   and open a real Hungarian product link from the deployed environment.

Thirty consented shopping tasks must still come from real participants. Public
product listings or fashion datasets cannot stand in for participant consent,
usefulness labels, and task-completion evidence. The [evaluation guide](task-evaluation-guide.md)
describes the private, pseudonymous report format.

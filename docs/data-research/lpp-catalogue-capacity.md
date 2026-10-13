# LPP storefront capacity

Measured 2026-10-13 from the local development network, against code at `9a554f5`.
These are access and size observations, not permission to reuse retailer content.

The five LPP-group storefronts that already work in the collector (Reserved, Sinsay,
Mohito, House, Cropp) list about **46,000 distinct products**. Collecting all their
clothing takes about **216 listing requests, roughly 48 minutes** one shop at a time,
or about 14 minutes with shops in parallel. Three code limits block a full run today;
see [Blockers](#blockers-in-the-current-code).

## Method

- Products are distinct SKU-colour pairs from each shop's Hungarian product sitemap
  (`https://static.<brand>.com/sitemaps/sitemap.xml.gz` →
  `sitemap_hu_hu_products_*.xml.gz`), minus entries robots.txt disallows
  (`/*Ntest*`).
- Category trees and live counts come from one category page per shop
  (`window.getCatalogData`: `productsQuantity`, `pageSize`, `maxPage`).
- Collection time = requests × 12 s (10 s robots crawl-delay plus transfer)
  + 4.5 ms per listed entry. The response-time model was measured on Mohito only:
  200 entries took 2.24 s; 1,000 entries (`?page=5`, 31.7 MB raw, 1.5 MB compressed)
  took 5.83 s.
- Photo counts are products × distinct photos per product from the image sitemaps
  (`sitemap_hu_hu_images_1`); House uses the gallery average. Disk assumes 2 photos
  per offer at 127.5 KB (one 1440×1920 Sinsay JPEG).
- Requests made: 43 in total, 8–9 per shop, at least 11 s apart per host; no 403 or
  429 anywhere.

## Per shop

| Shop | Sitemap URLs → products | Changed ≤7 d / ≤30 d | Measured listing | Proposed URLs → requests, time | Photos available / at 2 per offer, disk |
|---|---|---|---|---|---|
| Reserved | 18,851 → 12,169 | 19.7 % / 92.7 % | `/noi/ruhazat` is a 23-item promo page; no combined listing | 28 → ≤56, ~12.5 min | ~70k / 24.3k, 3.1 GB |
| Sinsay | 26,141 − 39 disallowed → 25,490 | 20.2 % / 86.2 % | `/noi/ruhazat` 5,204 (pageSize 120, maxPage 44) | 29 → ≤58, ~14 min | ~136k / 51.0k, 6.5 GB |
| Mohito | 4,242 → 2,954 | 14.1 % / 92.4 % | `/ruhazat` 2,824 (200 / 15) | 14 → ≤28, ~6 min | ~22k / 5.9k, 0.75 GB |
| House | 2,933 → 1,932, **stale** (last update 2024-03-05) | 0 % / 0 % | `/noi/ruhak` 1,616 (72 / 23) | 19 → ≤38, ~8 min | ~9k / 3.2k (women), 0.41 GB |
| Cropp | 4,475 → 3,741 | 8.5 % / 87.1 % | `/noi/ruhazat` 1,610 (100, then 36 / 43) | 18 → ≤36, ~7.5 min | ~21k / 7.5k, 0.95 GB |

Worked example: Reserved = 56 requests × 12 s = 11.2 min, plus
(12,169 + 28 × 200) entries × 4.5 ms ≈ 80 s, so about 12.5 minutes.

Totals: ~45,970 products (44,354 from sitemaps plus House's 1,616 live women's
items); 108 category URLs, ≤216 requests; ~258k photos available, ~91.9k at 2 per
offer, ~11.7 GB. At robots' 10 s crawl-delay (Sinsay sets none), photo download
takes about 150 hours.

Churn: 18.5 % of sitemap URLs changed in the last 7 days and 89.1 % in the last 30
(House excluded). `lastmod` bunches on bulk days (7,178 Sinsay URLs on 2026-09-30),
so the 7-day share is the better signal; a daily or weekly recollection fits.

## Proposed `--url` category lists

Paths under `https://www.<brand>.com/hu/hu/`. Promotion, new-in, trend and sale
nodes are left out to limit overlap.

- **Reserved:** `noi/{kabatok,puloverek,ruhak,ingek-bluzok,farmerek,nadragok,polok,blezerek-mellenyek,melegitofelsok,szoknyak,rovidnadragok,szettek}`; `ferfi/{dzsekik-kabatok,nadragok,farmerek,puloverek,ingek,felsok,polok,galleros-polok,zakok-blezerek,oltonyok}`; `{lany,fiu}/{5-13-eves,1-6-eves,bebi-0-12-honap}`
- **Sinsay:** `noi/ruhazat/{dzsekik,puloverek,melegito-felsok,nadragok,farmerek,ruhak,szoknyak,polok-es-topok,ingek,bluzok,rovidnadragok}`; `ferfi/ruhazat/{dzsekik,melegito-felsok,puloverek,nadragok,farmerek,polok,ingek,rovidnadragok}`; `gyermek/{k-outerwear,k-jogging-tops,k-trousers,k-jeans,k-tshirts,k-dresses,k-body,k-jumpsuits,shorts,sets}`
- **Mohito** (women only): `ruhazat/{kabatok,puloverek,farmer,ruhak-overalok,nadragok,polok-topok,bluzok,ingek,blezerek,szoknyak,rovidnadrag,melegitofelsok,oltonyok,szettek-eu}`
- **House** (no kids): `noi/ruhak/{kabatok-dzsekik,felsok,puloverek-kardiganok,nadragok,farmerek,polok-topok,ingek,ruhak-overallok,bluzok,szoknyak,rovidnadragok}`; `ferfi/ruhak/{kabatok-dzsekik,felsok,puloverek,nadragok,farmerek,polok,ingek,rovidnadragok}`
- **Cropp** (no kids): `noi/ruhazat/{dzsekik-kabatok,jogging-topok,farmerek,polok-topok,nadragok,puloverek,ruhak,ingek,szoknyak,sortok}`; `ferfi/ruhazat/{dzsekik-kabatok,jogging-topok,farmerek,polok,nadragok,puloverek,ingek,sortok}`

## Category mapping

`taxonomy.category_from_slug` already maps: kabatok → coats, dzsekik → jackets,
puloverek → sweaters, ruhak → dresses, farmer(ek) → jeans, nadragok → trousers,
szoknyak → skirts, rovidnadrag(ok) → shorts, ingek → shirts, bluzok → blouses,
felsok → tops.

Needs a manual mapping or new aliases in `taxonomy.ALIASES`:
- `polok`, `galleros-polok` (t-shirts, polo shirts)
- two-category slugs `dzsekik-kabatok`, `ingek-bluzok` (stay unclassified today)
- `blezerek`, `melegitofelsok`, `jogging-topok`
- `sortok` (shorts; not an alias yet)
- `oltonyok` (suits), `szettek` (sets)
- Sinsay `k-*` slugs and Reserved's age-group nodes

**Mislabel:** `source_for_page` labels House `/noi/ruhak` and `/ferfi/ruhak`
("clothes") as **dresses**, because `ruhak` is the dresses alias. Combined
`/ruhazat` listings stay unclassified, which is correct.

## Caveats

- Only one category per shop has a measured live count; sitemap totals also include
  shoes, accessories, underwear and (Sinsay) home goods.
- Sitemaps miss 10–19 % of live listing SKUs (Sinsay 108 of 120, Mohito 776 of 955,
  Cropp 90 of 100 found). The `products_new` sitemaps were not fetched. House's
  sitemap holds none of the 72 live SKUs checked, so collect House from listings.
- Sinsay has about 10 `testN` product URLs (e.g. `hand-cream-test6-00x`) that robots
  allows and `product_pattern` accepts. Do not filter on the word "test":
  `testhezallo` ("form-fitting") is a real product.
- robots.txt on the `static.` image hosts matches the `www` hosts and allows
  `/media/catalog/product/`. Crawl-delay is 10 everywhere except Sinsay. Cropp's
  robots.txt lists its sitemap without a `Sitemap:` key.

## Blockers in the current code

1. **TDMRep redirect misread (bug).** `/.well-known/tdmrep.json` on Reserved, Mohito,
   House and Cropp answers **HTTP 301 with no `Location` header and a 404 page
   body**; Sinsay answers 404. urllib raises that 301 as `HTTPError`, and
   `image_fetch.TdmRules.reserved` treats non-4xx errors as "unreadable", so
   `fetch-images` would skip every photo from those four shops. A 3xx without a
   usable target, like a 4xx, means no file and therefore no reservation.
2. **1,000-offer cap per run.** `collectors.MAX_LIMIT` (checked in `crawl()` and
   `cli.py`) caps each source per run. Listing pages are cumulative, so every run
   collects the same first 1,000 entries, and a category over 1,000 never finishes.
   Options: per-URL limits, raising the cap, or an `--all-categories` option that
   reads the category tree's `PRODUCTS` nodes next to `lpp_catalogue`.
3. **Very large jumps.** On a combined listing, `lpp_next_page` jumps to the last
   page: about 200 MB raw for Sinsay's `/noi/ruhazat`, an estimated ~25 s, above
   Scrapy's `DOWNLOAD_TIMEOUT` of 20 s (unverified). Use per-category URLs.
4. **Labels.** Fix `source_for_page` and `taxonomy.ALIASES` for the slugs above.
5. **Photo limits.** `image_index.MAX_ENTRIES` is 20,000 photos against ~92k
   wanted. `fetch-images --delay` defaults to 2 s and ignores robots crawl-delay 10.

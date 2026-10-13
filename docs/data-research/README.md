# Data research and plan (October 2026)

Research run on 2026-10-13 against code at `9a554f5`. It answers one question: how
can Fashion Finder get much more product data, legitimately, and how would each
route plug into the existing pipeline? Four topic files hold the evidence:

| File | Question it answers |
|---|---|
| [lpp-catalogue-capacity.md](lpp-catalogue-capacity.md) | How much can the five working LPP shops give, and what blocks a full run? |
| [hungarian-shop-survey.md](hungarian-shop-survey.md) | Which other shops selling in Hungary could be added, and how? |
| [affiliate-feeds.md](affiliate-feeds.md) | How to obtain official product feeds, and what the importer needs |
| [public-datasets.md](public-datasets.md) | Which open datasets can test and compare the image search |

No shop's bot protection was bypassed or probed: a shop that blocked a request was
recorded as blocked. Live checks used the collector's user agent and obeyed
robots.txt (two small deviations are logged in the shop survey).

## What each route gives

| Route | Data | Time to first data | Main cost |
|---|---|---|---|
| Five LPP shops, all clothing categories | ~46,000 products, ~258k photos | Hours, after code fixes | Fix 3 blockers; photos take ~150 h at robots' crawl-delay |
| New shops by web collection | Answear, Sportano now; eCipő + Modivo (claimed 90k+ and 200k+) after one parser fix | Days | Source config, one `collectors.py` fix, tests |
| Official feeds (Dognet, Tradedoubler, CJ) | LPP brands, bonprix, ABOUT YOU, Answear, Decathlon, more | 1–3 weeks after a public site (estimate) | Public site with impresszum and privacy page; importer changes; image use needs advertiser consent |
| Open datasets | ABO (CC BY 4.0), GLAMI-1M (Apache-2.0, Hungarian) | Days | A benchmark harness; never a substitute for real offers |

## Plan

Each step is small enough to hand to an agent as written. Effort: S < half a day,
M about a day, L several days.

### Step 1: unblock the five LPP shops (S–M)

1. **Fix the TDMRep redirect bug** in `image_fetch.TdmRules.reserved`: treat an
   `HTTPError` with a 3xx code (no usable `Location`) like 4xx, meaning no file and
   no reservation. Keep 5xx and network errors as unreadable. Add a test with a 301
   and no `Location`. Without this, `fetch-images` skips every photo from Reserved,
   Mohito, House and Cropp.
2. **Fix currency suffix stripping** in `schema.parse_money`: only strip a suffix
   that matches the row currency (`Ft`/`HUF` for HUF, `EUR`/`€` for EUR); otherwise
   raise. Test: "29.99 EUR" with currency HUF must fail.
3. **Collect whole categories.** Either per-category runs (the `--url` lists in
   [lpp-catalogue-capacity.md](lpp-catalogue-capacity.md)) with a per-URL limit, or
   an `--all-categories` option that reads the `{"tree":[...]}` category nodes with
   `displayMode == "PRODUCTS"` next to `collectors.lpp_catalogue`. Replace the
   per-run `MAX_LIMIT` of 1,000 with a per-category limit so cumulative paging can
   finish a large category.
4. **Fix labels.** Stop `source_for_page` labelling House `/ruhak` as dresses; add
   aliases for `polok`, `galleros-polok`, `blezerek`, `melegitofelsok`,
   `jogging-topok`, `sortok`, `oltonyok`, `szettek`, Sinsay `k-*` slugs and
   Reserved age groups.
5. **Respect crawl-delay for photos.** Read the image host's robots `Crawl-delay` in
   `image_fetch` and use the larger of it and `--delay`. Raise or shard
   `image_index.MAX_ENTRIES` (20,000) if indexing more than ~10k offers.

Then run a full collection of the five shops (~48 minutes) and a photo fetch for one
category to check the end-to-end flow before fetching all photos.

### Step 2: add the easiest new shops (S, then M)

- Sportano: source entry in `sources.py`, `removal.py`, the policy example, and a
  fixture test from a saved product page.
- Answear: switch from feed-only to web collection only if the owner agrees.
- eCipő and Modivo: in `collectors._product_node`, accept `ProductGroup`; on
  category pages, ignore priceless `Product`/`AggregateOffer` nodes so
  `parse_page` follows product links; strip the " | Ecipo.hu" title suffix.
- Re-check Zalando and Deichmann with a live product URL before deciding.

### Step 3: official feeds (owner action, then M–L)

1. Publish a minimal public site: what the service does, an impresszum, a privacy
   page, and an affiliate-link disclosure.
2. Apply to **Dognet** (individuals allowed; LPP brands including Sinsay, plus
   Decathlon) and **Tradedoubler** (individuals 18+; bonprix HU and ABOUT YOU HU
   allow price comparison). CJ via VIVnetworks is a third option.
3. Importer changes (details in [affiliate-feeds.md](affiliate-feeds.md)): an
   `affiliate_url` column, variant grouping by `item_group_id`, `sale_price` →
   `price` fallback, availability normalisation, gender and Google category IDs,
   ZIP and streaming parsing for large feeds, and skip-and-report instead of failing
   the whole feed.
4. Keep `allow_image_processing` false for each feed source until that advertiser
   agrees in writing.

### Step 4: measure with public data (M)

Build `scripts/benchmark_public_retrieval.py` (Script B in
[public-datasets.md](public-datasets.md)) on ABO apparel and GLAMI-1M `hu` rows, and
report Hit@5, MRR@10, nDCG@10 and Recall@1/10 for photo and look search. Use
LookBench privately to compare models. Only then consider a linear adapter or a
model change.

## Legal reminders

From the October 2026 review (not legal advice): the EU text-and-data-mining
exception needs lawful access and no machine-readable opt-out, and copies may be
kept only as long as needed. Photos of models are personal data, so crop or blur
faces before training. Link to retailer images rather than hosting copies. If the
site goes public, explain the ranking, label affiliate links, and never compute a
discount from the stored original price (the 30-day-low rule).

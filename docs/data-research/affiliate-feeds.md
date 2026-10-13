# Affiliate and merchant product feeds

Researched 2026-10-13 from public pages only; nothing was signed up for or
submitted. Code references are to `9a554f5`. This complements the
[candidate list](../hungarian-source-candidates.md) and the
[partner access handoff](../partner-access-handoff.md).

**Summary.** Feeds can be obtained legitimately, but **every network needs a public
website first**; a local-only prototype will not be approved. Examples: bonprix HU
requires a live, Hungarian-language site with an impresszum that is not under
construction [T3]; Dognet expects "an online project running" [D2]; Awin asks for
the site URL [A2]; CJ ties the links in an export to the selected website [V1].
Step 0 for every route is therefore a minimal public site with a description of the
service, an impresszum and a privacy page (inferred).

## Ranked routes

### 1. Dognet (Slovak network, active in Hungary)
- **Individuals:** yes. A natural person can be a partner, and "comparison portal"
  is a named partner type (GTC 1.6) [D1]. Non-entrepreneurs bill by e-mail;
  entrepreneurs invoice (8.6). Minimum payout €100 (8.5) [D1].
- **Hungarian fashion with "XML feed" ticked** [D3]: Reserved, Sinsay, Mohito and
  Cropp (LPP; integrated via a third party; the advertiser approves each join),
  Decathlon.hu, Norafashion.hu, Miadresses.hu, Focicipobolt.hu, Exisport.hu,
  Tchibo.hu.
- **Rules** [D1]: use feeds from at least 3 advertisers; update offers at least
  every 7 days; keep all link parameters; show current prices. The LPP campaigns
  forbid Google CSS but allow content sites [D3].
- **Access:** an "XML feed" tab after login; format and refresh rate are behind the
  login [D4].

### 2. Tradedoubler
- **Individuals:** yes, aged 18+ (3.4). Self-billing; you pay your own tax
  (5.1, 5.7). Threshold €50 (5.3) [T1].
- **Hungarian programmes** [T3]:
  - bonprix HU: daily feeds, **price comparison allowed**.
  - ABOUT YOU HU: product feed, **price comparison allowed** (since 2026-06-22).
  - Gomez.hu: feed, price comparison allowed.
  - Stradivarius HU, Lacoste HU: price comparison allowed; feed not stated.
  - Reserved, Sinsay, Cropp HU: feed available; CSS, retargeting and AdWords
    forbidden; price comparison not mentioned.
- **Access** [T2]: Products API with a token, JSON or XML. `productsUnlimited`
  returns the full feed, 3 downloads per file version per 24 hours after a 5-day
  grace period. `productUrl` is the tracked link; `sourceProductUrl` the plain
  retailer link. Not the Google format.

### 3. CJ via VIVnetworks
- **Individuals:** unclear; the publisher agreement is behind signup. Third-party
  guides mention W-8BEN for non-US individuals (unverified).
- **Payment:** 150 currencies, Payoneer, two payouts a month [C3]; thresholds not on
  public pages.
- **Feeds** ("XML termékfeed ✅") [V2]: ABOUT YOU, Answear, Reserved, Mohito, Cropp,
  HouseBrand. Sinsay is not on VIVnetworks (its page returns 404). None of these
  pages mentions price comparison.
- **Export** [V1]: Account > Subscriptions > Create product export, in Shopping
  (Google), XML or CSV format, delivered as ZIP over CJ HTTP/S (URL e-mailed within
  about an hour) or SFTP, on a schedule.
- **API** [C2]: GraphQL at `ads.api.cj.com/query`, up to 10,000 products per query,
  filterable by Google category. `link` is the landing page;
  `linkCode(pid).clickUrl` the tracking link. The API terms ban reselling access;
  no caching clause was found [C4].

### 4. Awin
- **Individuals:** yes, 18+ (1.5, 1.7) [A1]. Threshold €20 [A3].
- **Licence:** publish advertiser materials "without modification", only to market
  the advertiser (10.1); remove them on request (4.6) [A1].
- **Hungarian fashion:** only CCC HU found; its profile does not state a feed [A4].
  Downloads are CSV or XML [A5].

### 5. eHUB and Admitad
- eHUB: an individual without a business must be a Czech tax resident; an
  egyéni vállalkozó (sole trader) qualifies. Payout from 1,000 CZK [E1]. No
  Hungarian fashion campaign found [E2].
- Admitad: XML, CSV or Google Merchant exports, updated daily, with SubID tracking
  [M1]. Hungarian fashion advertisers unconfirmed (catalogue behind login).

### Direct routes
- The modivo.hu affiliate page was indexed earlier but returns 404 today.
- Fashion Days HU is live but shows no affiliate link [F1].
- No Hungarian programme found for Deichmann or C&A.
- Árukereső is itself a Dognet advertiser with an XML feed, but its links go to
  arukereso.hu, not the retailer [D3]; no evidence it shares merchant data
  (inferred).
- Google Merchant Center service accounts reach only your own account [G4];
  Shopping data is not open to third parties.

## Images, caching and timeline

- No network term read grants **image processing**. Awin's "without modification …
  to market" wording suggests photo similarity search needs explicit advertiser
  consent (inferred). Keep `allow_image_processing` false for feed sources until
  each advertiser agrees in writing.
- Local copies look expected: Dognet's 7-day refresh and Tradedoubler's download
  caps both imply a local catalogue.
- No network publishes approval times. Tradedoubler lets you apply to programmes
  as soon as you are approved [T4]; a CJ export URL arrives within about an hour
  [V1]. Estimate: 1–3 weeks from a live site to a first feed (inferred).
- Tax (inferred, not researched): an egyéni vállalkozó likely needs an EU VAT number
  for reverse-charge commissions. Check with an accountant.

## Field mapping: Google Shopping → our CSV

The CSV contract is `feed.CANONICAL_COLUMNS` (read by `feed._read_feed`);
`feed_mapping.map_xml_feed` maps XML to it.

| Google field | Our column | Change needed |
|---|---|---|
| `id` | `sku` | Variants sharing a link but with different ids raise "canonical URL already belongs to a different source product ID" in `storage.upsert_offer`; distinct variant links [G3] create duplicate offers. Group variants by `item_group_id` in `map_xml_feed`. |
| `item_group_id` | — | Use as the grouped offer's `sku`. |
| `title` | `name` | Works. |
| `link` | `product_url` | Tracking links fail `sources.allowed_url` in `feed._read_feed`. Add an `affiliate_url` column (`CANONICAL_COLUMNS`, `_read_feed`, `schema.RetailerOffer`, model, migration, API), keep `product_url` as the plain retailer URL. |
| `image_link` | `image_url` | Works. |
| `additional_image_link` | — | `_value` takes only the first element; CJ packs up to 10 comma-separated URLs [C1]. Add a list column and split it. |
| `sale_price` / `price` | `price` / `original_price` | `_mapping` allows one path per field, so "sale_price, else price" cannot be expressed. Add ordered fallback paths; set `original_price` only when on sale, honouring `sale_price_effective_date` [G1]. In Google feeds `price` is the **regular** price. |
| currency (inside `price`) | `currency` | **Bug (confirmed in code):** `schema.parse_money` strips any `Ft`, `HUF`, `EUR` or `€` suffix without comparing it to the row currency, so "29.99 EUR" with currency HUF imports as 29.99 HUF. Reject a suffix that does not match. |
| `availability` | `availability` | `schema.normalize` does not recognise CJ's "in stock" [C1] or Tradedoubler's "In Stock" [T2]; both become `unknown`. Normalise case and spaces; decide on preorder and backorder [G2]. |
| `color` / `size` | `colour` / `size` | CJ uses "Red/Green" [C1]; `normalize` does not split. Sizes need combining across variants. |
| `gtin`, `gender`, `age_group` | — | New columns. Gender matters for "women's jackets"; CJ values are male, female, unisex [C1]. |
| `google_product_category`, `product_type` | `category` | `taxonomy.normalize_category` matches whole strings only. Add a Google-category-ID table and match on the last breadcrumb segment. |
| (download time) | `observed_at` | Pass `--observed-at` with the download time (inferred). |
| `brand`, `description` | same | Work. |

## Other code limits

- `feed.MAX_FEED_BYTES` is 20 MB and `map_xml_feed` parses the whole tree; ABOUT YOU
  lists 500,000+ products [T3]. Needs ZIP handling, streaming parsing
  (`iterparse`) and a category filter, or the CJ API's category filter.
- `map_xml_feed` fails the whole feed when any row is invalid, because
  `validate_feed` requires zero errors. Large feeds need skip-and-report.
- New retailers need entries in `sources.SourceKey` and `SOURCES`, plus
  `market_paths` for hosts without a `/hu/` path.
- Tradedoubler XML needs its own mapping (`offers/offer/sourceProductUrl`,
  `priceHistory/price/@currency`). The price-history order is not specified [T2],
  so taking the first price may pick an old one.

## Sources

- V1 https://www.vivnetworks.com/en/product-feeds-on-cj-com/
- V2 https://www.vivnetworks.com/hu/affiliate-katalog/{aboutyouhu,answearhu,reserved-hu,mohito-hu,cropp-hu,housebrand-hu}/
- C1 https://developers.cj.com/docs/data-imports/product-feeds
- C2 https://developers.cj.com/graphql/reference/Product%20Feed
- C3 https://www.cj.com/publisher
- C4 https://www.cj.com/legal/ws-terms
- T1 https://publishers.tradedoubler.com/files/legal/en-publisher-agreement.pdf
- T2 https://dev.tradedoubler.com/products/publisher/
- T3 https://directory.tradedoubler.com/en/programs/{323187-Bonprix-HU,403594-About-You-HU,361080-Gomez-hu,392587-Stradivarius-HU,407860-Lacoste-HU,307352-Reserved-HU,307355-SINSAY-HU,307351-Cropp-HU}
- T4 https://www.tradedoubler.com/faq-publishers
- D1 https://www.dognet.sk/vop/vop-en-26092023.pdf
- D2 https://www.dognet.com/faq/
- D3 https://www.dognet.hu/kampanyok/{reserved-com,sinsay-com,mohito-com,cropp-com,decathlon-hu,norafashion-hu,miadresses-hu,focicipobolt-hu,exisport-hu-hu,tchibo-hu,arukereso-hu}/
- D4 https://www.dognet.com/xml-feeds/
- A1 https://www.awin.com/docs.awin.com/Legal/Publisher+Terms/2025/EN-(UK)_Awin+AG-Publisher-terms_August-2025.pdf
- A2 https://success.awin.com/articles/en_US/Knowledge/How-do-I-join-Awin-as-a-Publisher
- A3 https://success.awin.com/articles/en_US/Knowledge/What-are-the-payment-thresholds (search snippet)
- A4 https://ui.awin.com/merchant-profile/116737
- A5 https://developer.awin.com/docs/downloading-feeds-using-create-a-feed (search snippet)
- E1 https://ehub.cz/hu/kiadoi-kezikonyv
- E2 https://ehub.cz/hu/katalogus
- M1 https://admitad.useresponse.com/knowledge-base/article/product-feed-what-it-is-and-how-to-export-it-for-your-website_4
- F1 https://www.fashiondays.hu/
- G1 https://support.google.com/merchants/answer/6324471
- G2 https://support.google.com/merchants/answer/6324448
- G3 https://support.google.com/merchants/answer/6324507
- G4 https://developers.google.com/shopping-content/guides/how-tos/service-accounts

Not readable (behind login): CJ's publisher agreement and approval criteria;
Dognet feed formats; the Admitad and Awin catalogues; CityAds (the Sinsay HU
listing there is unconfirmed).

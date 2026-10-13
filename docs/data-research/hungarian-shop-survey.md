# Hungarian fashion shop survey

Live checks on 2026-10-13, 12:26–12:33 UTC, from the local development network,
against code at `9a554f5`. 28 shops were checked with at most about 4 requests each
(robots.txt, sitemap, one allowed page, `/.well-known/tdmrep.json`), using the
collector's user agent. Where a shop blocked or challenged us, checking stopped.
These are access observations, not permission to reuse content.

**Summary.** Two shops need configuration only: **Answear** and **Sportano**.
**eCipő and Modivo** (one platform) need one parser fix and hold the largest
catalogues. Five shops blocked us. No shop publishes a TDMRep file, and no response
carried a `tdm-reservation` header.

## Ranked results

| # | Shop | Verdict | Evidence | Est. products | Effort and files |
|---|---|---|---|---|---|
| 1 | Answear | ready | robots allows `/p/`; product page has Product JSON-LD with one offer (64990 `HUF`) | 7 product sitemaps (count not fetched) | S: `sources.py` (category URL, product pattern `/p/…-\d+`, add `/k/` to `market_paths`). Feed-only by policy today; switching to web collection is the owner's decision. |
| 2 | Sportano | ready | Product JSON-LD, 4 offers at one price (29055 HUF), plus `product:price:amount` | 23 product sitemaps | S: `sources.py`, `removal.py`, `config/source-policies.example.json` |
| 3 | eCipő + Modivo | adapter fix | Product JSON-LD is `ProductGroup`, which `_product_node` does not match; `extract_product` still works via meta (77890 HUF), but the name keeps " \| Ecipo.hu" and brand is empty. Modivo category pages carry a priceless category-level `Product`/`AggregateOffer`, which is returned as a record, so `parse_page` never follows the 72 `/p/` links. | Modivo jackets: `offerCount` 2,271; sites claim eCipő >90k, Modivo >200k (search snippets) | M: `collectors.py` (accept `ProductGroup`; reject priceless/`AggregateOffer` nodes on category pages), source entries, tests |
| 4 | Footshop | feed only (or adapter) | No JSON-LD or price meta; the HUF price is in Astro `productData` props | 1 product sitemap | S with its CJ feed via `map-xml-feed`; M as an adapter |
| 5 | Sports Direct HU (formerly Hervis) | adapter | JSON-LD types are localised (`"Termék"`, `"Ajánlat"`); 21 offers with 4 distinct prices such as "24750,0" | women's jackets ~914 (snippet) | M: `collectors.py` and source files |
| 6 | Stradivarius | adapter, fragile | `/hu/v/bor-dzseki` returned 200 with an ItemList of 20 Products, but each has `priceCurrency: "EUR"` on HUF amounts (35995.00). Sister brands block us. | 1 HU product sitemap | M: `collectors.py` |
| 7 | CCC | adapter | Angular server-rendered page; `/hu/hu/p/` links but no prices or JSON-LD in HTML | 684 HU categories | L (browser mode and a product-page check) |
| 8 | Zalando | unverified | robots allows `*` but disallows GPTBot, CCBot, ClaudeBot and Google-Extended; the sampled product URL had expired (404) | unknown | re-check with a live URL |
| 9 | Deichmann | unverified | sampled product URL had expired (404); a HU sitemap index exists | unknown | re-check |
| 10 | ABOUT YOU | feed only | product page is a client-rendered shell (`"serverRenderingMode":"csr"`) | 8 product sitemaps | existing feed path |

**Blocked (checking stopped):** Decathlon (product page 403); Fashion Days
(robots.txt returned a Tencent EdgeOne CAPTCHA page); Mango (sitemap 429); Bershka
(Akamai `bm-verify` challenge); Sportisimo (403 Cloudflare challenge on
`tdmrep.json`; its robots.txt also sets `ai-train=no`, an Article 4 reservation).

**Skip:**
- bonprix: robots disallows `/termek/*` (product pages) and has `User-agent: es` →
  `Disallow: /`. Protego may substring-match `es` against our user agent and block
  everything (inferred). Use its Tradedoubler feed instead
  ([affiliate feeds](affiliate-feeds.md)).
- C&A: no HU shop sitemap; Hungary appears to use the EUR `/eu/en` shop.
- Humanic, Tally Weijl: no HU locale. P&C: stores and loyalty pages only.
  New Yorker: redirects to a brand page. Orsay: left Hungary in 2022.
  LC Waikiki: lcw.com is Turkey only. KiK: corporate site only.
  Bibloo: redirects to an EUR Shopify store. Notino: beauty (not checked).
- SHEIN: its terms ban "robots, spiders or other automated devices" (Swiss/German
  terms, translated; EU text not confirmed); no `/hu/` sitemap.

## Top five to add

1. **Answear**: configuration only (owner decision, see above).
2. **Sportano**: clean JSON-LD and robots; mostly sportswear.
3. **eCipő and Modivo**: one fix in `collectors.py` opens two large catalogues.
4. **Footshop**: use its XML feed rather than an adapter.
5. **Sports Direct HU**: large catalogue; needs a parser for its localised JSON-LD.

## Terms and affiliate notes

From web search; full terms were not read because of the request cap.

- Zalando ÁSZF 1.8 reserves the right to refuse orders placed "szoftver, kereső vagy
  bármilyen más robot…" (by software, search engines or other robots). It covers
  orders, not browsing. [zalando.hu/zalando-terms](https://www.zalando.hu/zalando-terms)
- SHEIN: [m.shein.com/ch/product/article/399](https://m.shein.com/ch/product/article/399/)
- No automated-access clause in search excerpts for
  [eCipő](https://ecipo.hu/b/felhasznalasi-szabalyzat),
  [Modivo](https://modivo.hu/b/felhasznalasi-szabalyzat-09-02-2026),
  [Sportano](https://sportano.hu/aruhaz-szabalyzat) and
  [ABOUT YOU](https://www.aboutyou.hu/aszf). No Answear terms page was found.
- Footshop.hu: [VIVnetworks](https://www.vivnetworks.com/hu/affiliate-katalog/footshophu/)
  lists "XML termékfeed ✅"; the join link points to CJ (network inferred).
- eCipő: on Dognet per the [affi.io](https://affi.io/m/ecipo) aggregator; no feed
  mentioned. Modivo.hu appeared in a VIVnetworks search result, but
  [that page](https://www.vivnetworks.com/hu/affiliate-katalog/modivohu) now returns 404.
- Hervis became Sports Direct:
  [telex](https://telex.hu/gazdasag/2026/07/03/hervis-felvasarlas-tranzakcio-sports-direct-uzlet).

## Request log and rule deviations

76 requests, all with the collector's user agent. Most hosts received 1–4.
Deviations from the survey rules, recorded for transparency:
- `curl -L` followed 14 redirect hops without the 5 s gap (including 3 on ccc.eu and
  2 on www.ecipo.hu → ecipo.hu).
- robots.txt was read on www.ecipo.hu (404, all allowed), but the sitemap and
  product page were served from bare `ecipo.hu`, whose robots.txt was not fetched.
- Sitemaps named in robots.txt on other hosts (sitemaps.footshop.hu,
  shop.mango.com, storage.googleapis.com) were fetched without checking those
  hosts' robots.txt.

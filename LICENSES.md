# Third-party software inventory

Point-in-time engineering review, 2026-10-12. Re-check before copying code or redistributing the project.

| Component | Repository | Observed licence | Decision |
|---|---|---|---|
| Scrapy | https://github.com/scrapy/scrapy | BSD-3-Clause | Use as dependency; preserve required notices |
| Playwright Python | https://github.com/microsoft/playwright-python | Apache-2.0 | Use as dependency; comply with Apache notice conditions |
| Crawlee Python | https://github.com/apify/crawlee-python | Apache-2.0 | Benchmark candidate; not required at runtime initially |
| ZaraDataScraper | https://github.com/BRI4MZP/ZaraDataScraper | Apache-2.0 | Candidate extraction code; inspect/modernize dependencies before copying |
| scraperClothesHM | https://github.com/SebaGGu619/scraperClothesHM | MIT | Candidate extraction code; update for current pages before copying |
| reserved.com-scraper | https://github.com/bopopescu/reserved.com-scraper | Apache-2.0 | Candidate; inspect full source and compatibility before copying |
| pull-bear | https://github.com/byteer228eaglepro/pull-bear | No explicit licence found in review | Do not copy without permission; implement an independent adapter |
| google-shopping-scraper | https://github.com/data-scrape/google-shopping-scraper | MIT | Optional experiment; inspect dependencies and output |
| Crinibus/scraper | https://github.com/Crinibus/scraper | MIT | Reference for price-history structure |
| Phosphor Icons (@phosphor-icons/core 2.1.1) | https://github.com/phosphor-icons/phosphor-core | MIT | Added 2026-11-02: regular-weight icon paths copied into `web/app/_components/icons.tsx` and the select caret in `web/app/globals.css`; keep the MIT notice in `icons.tsx` |
| Geist typeface | https://github.com/vercel/geist-font | SIL Open Font License (licence URL in the font's name table) | Added 2026-11-02: loaded with `next/font/google` at build time and self-hosted with the web build; also the default font of `next/og` for share images |

This is not legal advice. The table records source-code licence observations only; these licences do not grant rights to collected retailer data, product photos or website access.

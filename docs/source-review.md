# Source and Licence Review

Last reviewed: 2026-10-12. These are point-in-time observations of public repository files. Re-check the actual repository and licence before importing code, since projects can change.

## General-purpose tools

| Tool | Repository | Observed licence | Proposed use |
|---|---|---|---|
| Scrapy | https://github.com/scrapy/scrapy | BSD-3-Clause | Primary HTTP crawler framework |
| Playwright for Python | https://github.com/microsoft/playwright-python | Apache-2.0 | Browser automation for rendered pages |
| Crawlee for Python | https://github.com/apify/crawlee-python | Apache-2.0 | Alternative framework to benchmark |

All three licences permit use and modification subject to their conditions and notice requirements.

## Retailer-specific community projects

### Zara
- Repository: https://github.com/BRI4MZP/ZaraDataScraper
- Observed licence: Apache-2.0 (LICENSE present).
- README describes a Python/Selenium product scraper that exports data to CSV.
- requirements.txt pins old Selenium and pandas versions. Inspect and modernize dependencies; do not install blindly.

### H&M
- Repository: https://github.com/SebaGGu619/scraperClothesHM
- Observed licence: MIT (LICENSE present).
- README and main.py describe an older collector targeting Romanian H&M pages and saving gathered HTML for later processing.
- Treat this as sample extraction logic, not a confirmed current full-catalogue collector. Re-test and replace outdated parsing.

### Reserved
- Repository: https://github.com/bopopescu/reserved.com-scraper
- Observed licence: Apache-2.0 (LICENSE present).
- README describes scraping items from a provided URL and saving records; it mentions BeautifulSoup and MySQL Connector.
- Inspect all source files and current page compatibility before porting it.

### Pull&Bear
- Candidate repository: https://github.com/byteer228eaglepro/pull-bear
- No explicit LICENSE file was found during this review.
- Decision: do not copy or adapt its code unless permission is obtained. Implement an independent adapter with Scrapy/Playwright.

### Optional Google Shopping experiment
- Repository: https://github.com/data-scrape/google-shopping-scraper
- Observed licence: MIT (LICENSE present).
- Proposed use: experiment with shopping-search results as product candidates.
- This is not guaranteed to provide complete retailer catalogues or authoritative current stock. Inspect dependencies, output quality and applicable terms before use.

### Optional price-history reference
- Repository: https://github.com/Crinibus/scraper
- Observed licence: MIT (LICENSE present).
- A Python price tracker with historical observations and visualizations. Use as a design reference; do not assume its existing store adapters work for our target retailers.

## Reuse rules

1. Public visibility is not a licence. Do not reuse code without an explicit licence or written permission.
2. Preserve required copyright and licence notices for copied or modified code.
3. Re-check dependency licences before adding them.
4. Keep source-code licences separate from retailer content rights and site-access terms.
5. An extractor's ability to obtain a field does not establish permission to store, display, republish or process it.
6. This is an engineering inventory, not legal advice. It reviews code licensing, not permission to collect or republish retailer data.

## Addendum 2026-10-24: collector framework notes

- No new runtime dependency was added. The collector still uses Scrapy (BSD-3-Clause),
  Protego (BSD-3-Clause) and lxml through Scrapy; the optional `crawlee-benchmark` extra is
  unchanged. `actionlint` was used locally to lint the workflow and is not a dependency.
- Scrapy 2.19's stock `RobotsTxtMiddleware` shares one Twisted `Deferred` between all
  requests waiting for a host's robots.txt. Under the asyncio reactor each waiter calls
  `Deferred.asFuture`, which passes the result to the first waiter only and `None` to the
  others, and `None` means "no rules". `politeness.PoliteRobotsMiddleware` replaces the
  waiting with an asyncio future so every request is checked; see [the scraper
  guide](scraper.md#compliance-model). Worth reporting upstream after a minimal reproduction.

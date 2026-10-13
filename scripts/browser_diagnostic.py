"""Check one storefront in an ordinary Chromium session without collecting data.

Install the package and Chromium first, then run:
    python scripts/browser_diagnostic.py zara
"""

from __future__ import annotations

import argparse
import json
import re
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as BrowserTimeout
from playwright.sync_api import sync_playwright

from fashion_finder_data.sources import CRAWL_SOURCES, allowed_url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=CRAWL_SOURCES)
    args = parser.parse_args()
    source = CRAWL_SOURCES[args.source]
    result = {"source": source.key, "url": source.category_url}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                response = page.goto(
                    source.category_url, wait_until="domcontentloaded", timeout=15000
                )
                result["http_status"] = response.status if response else None
                result["title"] = page.title()
                if response and response.ok:
                    page.wait_for_timeout(1500)
                    hrefs = page.locator("a[href]").evaluate_all(
                        "elements => elements.map(element => element.getAttribute('href'))"
                    )
                    result["product_link_count"] = len(
                        {
                            url
                            for href in hrefs
                            if href
                            for url in [urljoin(page.url, href)]
                            if allowed_url(source, url)
                            and re.search(source.product_pattern, url, re.IGNORECASE)
                        }
                    )
                    body = page.locator("body").inner_text(timeout=3000)
                    result["huf_price_mentions"] = len(
                        re.findall(r"\b\d[\d\s,.]*\s*(?:Ft|HUF)\b", body, re.IGNORECASE)
                    )
            finally:
                browser.close()
    except BrowserTimeout:
        result["error"] = "browser navigation timed out"
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("http_status") == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())

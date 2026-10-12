"""Compare Scrapy and Crawlee on the same local HTTP fixture.

Run with: python scripts/benchmark_crawlers.py
The fixture measures framework overhead and extraction success, not retailer access.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import perf_counter
from urllib.parse import urljoin

PRODUCTS = {
    "one": ("Wool coat", "42995"),
    "two": ("Cotton dress", "19995"),
    "three": ("Linen shirt", "12995"),
}


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            content_type = "text/plain"
        elif self.path == "/category":
            body = (
                "<html><body>"
                + "".join(f'<a class="product" href="/product/{key}">{key}</a>' for key in PRODUCTS)
                + '<a class="product" href="/product/broken">broken</a>'
                + "</body></html>"
            ).encode()
            content_type = "text/html"
        elif self.path.startswith("/product/"):
            key = self.path.rsplit("/", 1)[-1]
            if key in PRODUCTS:
                name, price = PRODUCTS[key]
                body = f'<h1>{name}</h1><span class="price">{price}</span>'.encode()
            else:
                body = b"<h1>Unavailable data</h1>"
            content_type = "text/html"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


def run_scrapy(base_url: str) -> dict[str, object]:
    import scrapy
    from scrapy.crawler import CrawlerProcess

    offers: list[tuple[str, str]] = []
    failures = 0

    class FixtureSpider(scrapy.Spider):
        name = "fixture_benchmark"

        def start_requests(self):
            yield scrapy.Request(base_url + "/category", callback=self.parse_category)

        async def start(self):
            for request in self.start_requests():
                yield request

        def parse_category(self, response):
            for href in response.css("a.product::attr(href)").getall():
                yield response.follow(href, callback=self.parse_product)

        def parse_product(self, response):
            nonlocal failures
            name = response.css("h1::text").get()
            price = response.css("span.price::text").get()
            if name and price:
                offers.append((name, price))
            else:
                failures += 1

    process = CrawlerProcess(
        settings={
            "ROBOTSTXT_OBEY": True,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
            "DOWNLOAD_DELAY": 0,
            "RETRY_TIMES": 0,
            "LOG_LEVEL": "ERROR",
            "TELNETCONSOLE_ENABLED": False,
        }
    )
    process.crawl(FixtureSpider)
    process.start()
    return {"offers": sorted(offers), "incomplete": failures}


async def run_crawlee(base_url: str) -> dict[str, object]:
    from crawlee import ConcurrencySettings
    from crawlee.crawlers import ParselCrawler

    offers: list[tuple[str, str]] = []
    failures = 0
    crawler = ParselCrawler(
        max_requests_per_crawl=5,
        max_request_retries=0,
        concurrency_settings=ConcurrencySettings(max_concurrency=1, desired_concurrency=1),
        respect_robots_txt_file=True,
    )

    @crawler.router.default_handler
    async def handle(context):
        nonlocal failures
        if context.request.url.endswith("/category"):
            for href in context.selector.css("a.product::attr(href)").getall():
                await crawler.add_requests([urljoin(context.request.url, href)])
            return
        name = context.selector.css("h1::text").get()
        price = context.selector.css("span.price::text").get()
        if name and price:
            offers.append((name, price))
        else:
            failures += 1

    await crawler.run([base_url + "/category"])
    return {"offers": sorted(offers), "incomplete": failures}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner", choices=("scrapy", "crawlee"))
    parser.add_argument("--base-url")
    args = parser.parse_args()
    if args.runner:
        start = perf_counter()
        result = (
            run_scrapy(args.base_url)
            if args.runner == "scrapy"
            else asyncio.run(run_crawlee(args.base_url))
        )
        print(
            json.dumps(
                {"framework": args.runner, "seconds": round(perf_counter() - start, 3), **result}
            )
        )
        return
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        results = []
        with tempfile.TemporaryDirectory(prefix="fashion-finder-benchmark-") as storage_dir:
            child_env = {**os.environ, "CRAWLEE_STORAGE_DIR": storage_dir}
            for runner in ("scrapy", "crawlee"):
                completed = subprocess.run(
                    [sys.executable, __file__, "--runner", runner, "--base-url", base_url],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=child_env,
                )
                if completed.returncode:
                    raise SystemExit(f"{runner} failed: {completed.stderr[-3000:]}")
                results.append(json.loads(completed.stdout.strip().splitlines()[-1]))
        for result in results:
            print(json.dumps(result))
        if results[0]["offers"] != results[1]["offers"] or len(results[0]["offers"]) != 3:
            raise SystemExit("frameworks did not extract the same three complete offers")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()

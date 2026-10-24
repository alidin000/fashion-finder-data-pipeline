"""Run the polite Scrapy stack against a local fake shop; used by test_politeness.py.

Scrapy's reactor cannot restart inside one process, so each scenario runs this file
in a subprocess: ``python tests/polite_harness.py <base-url> <json-config>``. It
prints one JSON line with what the spider saw and the per-source telemetry.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import scrapy
from scrapy.crawler import CrawlerProcess

from fashion_finder_data.crawl_state import CrawlStore
from fashion_finder_data.politeness import Telemetry, polite_settings
from fashion_finder_data.sources import Source, user_agent


class HarnessSpider(scrapy.Spider):
    name = "polite_harness"

    def __init__(self, base: str, paths: list[str], telemetry: Telemetry, seen: list, **kwargs):
        super().__init__(**kwargs)
        self.base = base
        self.paths = paths
        self.telemetry = telemetry
        self.seen = seen

    async def start(self):
        for index, path in enumerate(self.paths):
            yield scrapy.Request(
                self.base + path,
                callback=self.parse,
                errback=self.failed,
                meta={"source_key": "shop", "kind": "product", "conditional": True},
                priority=-index,  # keep the configured order
                dont_filter=True,
            )

    def parse(self, response):
        self.seen.append({"path": response.url[len(self.base) :], "status": response.status})

    def failed(self, failure):
        self.seen.append(
            {
                "path": failure.request.url[len(self.base) :],
                "error": f"{type(failure.value).__name__}: {failure.value}",
            }
        )


def main() -> None:
    base, config = sys.argv[1], json.loads(sys.argv[2])
    source = Source("shop", "127.0.0.1", None, None, None)
    store = CrawlStore(Path(config["store"])) if config.get("store") else None
    telemetry = Telemetry(
        [source],
        max_requests=config.get("max_requests", 50),
        min_delay=config.get("min_delay", 0.05),
        store=store,
    )
    settings = polite_settings(
        user_agent=user_agent("https://example.org/contact"),
        min_delay=config.get("min_delay", 0.05),
        timeout=5,
    )
    settings["LOG_LEVEL"] = "ERROR"
    seen: list = []
    process = CrawlerProcess(settings=settings)
    started = time.monotonic()
    process.crawl(HarnessSpider, base=base, paths=config["paths"], telemetry=telemetry, seen=seen)
    process.start()
    stats = telemetry.by_source["shop"]
    print(
        json.dumps(
            {
                "seen": seen,
                "telemetry": stats.as_dict(),
                "floors": telemetry.floors,
                "elapsed": time.monotonic() - started,
            }
        )
    )


if __name__ == "__main__":
    main()

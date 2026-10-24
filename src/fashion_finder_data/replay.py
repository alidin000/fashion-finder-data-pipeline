"""Serve recorded responses instead of the network, for tests and offline rehearsals.

A replay directory holds ``index.json``::

    {"https://www.reserved.com/robots.txt": {"status": 200, "body": "robots.txt",
      "headers": {"Content-Type": "text/plain"}}}

Bodies are files next to the index. Unknown URLs answer HTTP 404, like a site
without that file. The whole politeness stack (robots.txt, TDMRep, delays,
challenge detection) still runs; only the download is replaced, so nothing
leaves the machine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from scrapy.core.downloader.handlers.base import BaseDownloadHandler
from scrapy.http import Headers, Request, Response
from scrapy.responsetypes import responsetypes

REPLAY_ENV = "FASHION_FINDER_REPLAY_DIR"


class ReplayDownloadHandler(BaseDownloadHandler):
    def __init__(self, crawler):
        super().__init__(crawler)
        directory = crawler.settings.get("FASHION_FINDER_REPLAY_DIR") or os.environ.get(REPLAY_ENV)
        if not directory:
            raise ValueError("replay needs FASHION_FINDER_REPLAY_DIR")
        self.root = Path(directory).resolve()
        self.index = json.loads((self.root / "index.json").read_text(encoding="utf-8"))

    async def download_request(self, request: Request) -> Response:
        entry = self.index.get(request.url)
        if entry is None:
            entry = {"status": 404, "headers": {"Content-Type": "text/plain"}, "text": "not found"}
        if isinstance(entry, list):  # successive answers, e.g. 429 then 200
            calls = getattr(self, "_calls", {})
            count = calls.get(request.url, 0)
            calls[request.url] = count + 1
            self._calls = calls
            entry = entry[min(count, len(entry) - 1)]
        if "body" in entry:
            path = (self.root / entry["body"]).resolve()
            if self.root not in path.parents:
                raise ValueError("replay body must stay inside the replay directory")
            body = path.read_bytes()
        else:
            body = str(entry.get("text", "")).encode()
        headers = Headers(entry.get("headers") or {})
        cls = responsetypes.from_args(headers=headers, url=request.url, body=body)
        request.meta["download_latency"] = float(entry.get("latency", 0.05))
        return cls(
            url=request.url,
            status=int(entry.get("status", 200)),
            headers=headers,
            body=body,
            request=request,
        )

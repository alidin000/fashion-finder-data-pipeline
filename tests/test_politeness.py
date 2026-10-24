"""The collector obeys robots.txt, crawl delays, Retry-After and stops when blocked.

Each scenario runs the real Scrapy middleware stack in a subprocess against a local
fake shop that records every request. No retailer is contacted.
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from fashion_finder_data.politeness import challenge_marker, retry_after_seconds, tdm_reserved

HARNESS = Path(__file__).with_name("polite_harness.py")
ROBOTS = b"User-agent: *\nDisallow: /private/\nCrawl-delay: 1\n"


class FakeShop:
    """Routes map a path to a list of (status, headers, body); the last one repeats."""

    def __init__(self, routes: dict[str, list[tuple[int, dict[str, str], bytes]]]):
        self.routes = routes
        self.log: list[dict] = []
        shop = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = self.path
                shop.log.append(
                    {"path": path, "at": time.monotonic(), "headers": dict(self.headers.items())}
                )
                answers = shop.routes.get(path) or [(404, {}, b"not found")]
                calls = sum(1 for entry in shop.log if entry["path"] == path)
                status, headers, body = answers[min(calls, len(answers)) - 1]
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def paths(self) -> list[str]:
        return [entry["path"] for entry in self.log]

    def times(self, path: str) -> list[float]:
        return [entry["at"] for entry in self.log if entry["path"] == path]


@pytest.fixture
def shop() -> Iterator:
    servers: list[FakeShop] = []

    def start(routes):
        fake = FakeShop(routes)
        threading.Thread(target=fake.server.serve_forever, daemon=True).start()
        servers.append(fake)
        return fake

    yield start
    for fake in servers:
        fake.server.shutdown()


def run(fake: FakeShop, paths: list[str], **config) -> dict:
    completed = subprocess.run(
        [sys.executable, str(HARNESS), fake.base, json.dumps({"paths": paths, **config})],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def page(body: str = "<html><body>ok</body></html>", **headers: str):
    return (200, {"Content-Type": "text/html", **headers}, body.encode())


def test_robots_rules_and_crawl_delay_are_obeyed(shop) -> None:
    fake = shop(
        {
            "/robots.txt": [(200, {"Content-Type": "text/plain"}, ROBOTS)],
            "/a": [page()],
            "/b": [page()],
            **{f"/private/{name}": [page()] for name in "cde"},
        }
    )
    # Every request below waits for robots.txt at once; Scrapy's stock middleware
    # would let all but the first waiter through unchecked.
    result = run(fake, ["/a", "/private/c", "/private/d", "/private/e", "/b"])
    assert not [path for path in fake.paths() if path.startswith("/private/")]
    assert result["telemetry"]["robots_disallowed"] == 3
    assert result["telemetry"]["crawl_delays"] == {"127.0.0.1": 1.0}
    sent = sorted(fake.times("/a") + fake.times("/b") + fake.times("/.well-known/tdmrep.json"))
    gaps = [later - earlier for earlier, later in itertools.pairwise(sent)]
    assert gaps and min(gaps) >= 0.95  # Crawl-delay: 1 beats the 0.05 s minimum delay
    user_agents = {entry["headers"].get("User-Agent") for entry in fake.log}
    assert user_agents == {"FashionFinderBot/0.1.0 (+https://example.org/contact)"}


def test_rate_limit_waits_for_retry_after_then_retries_once(shop) -> None:
    fake = shop(
        {
            "/robots.txt": [(404, {}, b"")],
            "/busy": [(429, {"Retry-After": "1"}, b"slow down"), page()],
        }
    )
    result = run(fake, ["/busy"])
    first, second = fake.times("/busy")
    assert second - first >= 0.95
    assert result["seen"] == [{"path": "/busy", "status": 200}]
    assert result["telemetry"]["backoffs"] == 1
    assert result["telemetry"]["stopped"] is None


def test_long_retry_after_stops_the_source_instead_of_waiting(shop) -> None:
    fake = shop(
        {
            "/robots.txt": [(404, {}, b"")],
            "/busy": [(503, {"Retry-After": "3600"}, b"maintenance")],
            "/later": [page()],
        }
    )
    result = run(fake, ["/busy", "/later"])
    assert "/later" not in fake.paths()
    assert "Retry-After 3600" in result["telemetry"]["stopped"]


def test_repeated_forbidden_stops_the_source(shop) -> None:
    fake = shop(
        {
            "/robots.txt": [(404, {}, b"")],
            **{
                f"/p{number}": [(403, {}, b"<html>" + b"x" * 20000 + b"</html>")]
                for number in range(5)
            },
        }
    )
    result = run(fake, [f"/p{number}" for number in range(5)])
    assert len([path for path in fake.paths() if path.startswith("/p")]) == 2
    assert result["telemetry"]["stopped"] == "HTTP 403 answered 2 times"


def test_challenge_page_stops_the_source_without_parsing(shop) -> None:
    challenge = "<html><title>Just a moment...</title><script>window._cf_chl_opt={}</script>"
    fake = shop(
        {
            "/robots.txt": [(404, {}, b"")],
            "/a": [page(challenge)],
            "/b": [page()],
        }
    )
    result = run(fake, ["/a", "/b"])
    assert "/b" not in fake.paths()
    assert result["seen"][0]["error"].startswith("IgnoreRequest")
    assert result["telemetry"]["stopped"].startswith("cloudflare challenge")


@pytest.mark.parametrize(
    ("robots", "allowed"),
    [((403, {}, b"forbidden"), False), ((500, {}, b"error"), False), ((410, {}, b""), True)],
)
def test_robots_status_decides_access(shop, robots, allowed) -> None:
    fake = shop({"/robots.txt": [robots], "/a": [page()]})
    result = run(fake, ["/a"])
    assert ("/a" in fake.paths()) is allowed
    assert (result["telemetry"]["stopped"] is None) is allowed


def test_tdm_reservation_stops_text_collection(shop) -> None:
    reservation = json.dumps([{"location": "/", "tdm-reservation": 1}]).encode()
    fake = shop(
        {
            "/robots.txt": [(404, {}, b"")],
            "/.well-known/tdmrep.json": [(200, {"Content-Type": "application/json"}, reservation)],
            "/a": [page()],
        }
    )
    result = run(fake, ["/a"])
    assert "/a" not in fake.paths()
    assert result["telemetry"]["stopped"] == "text-and-data-mining reserved (TDMRep)"


def test_request_budget_is_enforced_at_send_time(shop) -> None:
    fake = shop({"/robots.txt": [(404, {}, b"")], **{f"/p{n}": [page()] for n in range(6)}})
    # The budget counts every request to the site: robots.txt and tdmrep.json too.
    result = run(fake, [f"/p{n}" for n in range(6)], max_requests=4)
    assert len(fake.log) == 4
    assert len([path for path in fake.paths() if path.startswith("/p")]) == 2
    assert result["telemetry"]["budget_exhausted"] is True


def test_conditional_request_uses_stored_validators(shop, tmp_path) -> None:
    fake = shop(
        {
            "/robots.txt": [(404, {}, b"")],
            "/a": [page(ETag='"v1"'), (304, {"ETag": '"v1"'}, b"")],
        }
    )
    store = str(tmp_path / "state.sqlite")
    first = run(fake, ["/a"], store=store)
    second = run(fake, ["/a"], store=store)
    assert first["seen"] == [{"path": "/a", "status": 200}]
    assert second["seen"] == [{"path": "/a", "status": 304}]
    assert second["telemetry"]["not_modified"] == 1
    assert fake.log[-1]["headers"].get("If-None-Match") == '"v1"'


def test_retry_after_accepts_seconds_and_http_dates() -> None:
    assert retry_after_seconds(b"120") == 120
    assert retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT", now=1792567660) == 20
    assert retry_after_seconds("soon") is None


def test_tdmrep_first_matching_location_decides() -> None:
    rules = [
        {"location": "/media/*", "tdm-reservation": 0},
        {"location": "/", "tdm-reservation": 1},
    ]
    assert tdm_reserved(rules, "https://shop.example/media/a.jpg") is False
    assert tdm_reserved(rules, "https://shop.example/p/coat") is True


def test_recaptcha_on_a_normal_product_page_is_not_a_challenge() -> None:
    from scrapy.http import HtmlResponse

    body = "<html>" + "<p>Kabát</p>" * 3000 + '<div class="g-recaptcha"></div></html>'
    normal = HtmlResponse("https://shop.example/p", body=body.encode(), status=200)
    assert challenge_marker(normal) is None
    blocked = HtmlResponse("https://shop.example/p", body=body.encode(), status=403)
    assert challenge_marker(blocked) == "captcha or block page"

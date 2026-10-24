"""Politeness and access-control rules for Scrapy collection.

Three downloader middlewares replace Scrapy's robots.txt middleware and add the
rules a polite, honest crawler needs:

- ``PoliteRobotsMiddleware`` (priority 100) reads robots.txt with Protego under
  RFC 9309 status rules: 2xx is parsed; 404/410 and other 4xx mean "no rules";
  401/403, 429, 5xx, network errors and challenge pages mean "disallow all" and
  stop the source. It records ``Crawl-delay``/``Request-rate`` for each host and
  checks the host's TDMRep file (``/.well-known/tdmrep.json``) before collecting
  pages; a text-and-data-mining reservation stops the source.
- ``PoliteResponseMiddleware`` (580, after decompression and redirects) stops a
  source on a CAPTCHA or bot-challenge page, a ``tdm-reservation: 1`` header, or
  repeated HTTP 401/403; backs off on 429/503 using ``Retry-After``; and records
  ETag/Last-Modified validators for conditional requests.
- ``PoliteGateMiddleware`` (950, last before the network) allows one request per
  host at a time, spaces them by the largest of the configured delay, the
  robots.txt crawl delay and any Retry-After backoff, re-checks that the source is
  not stopped and within its request budget at the moment of sending, adds
  conditional request headers, and counts requests and status codes.

None of this evades blocking: there is no proxy rotation, header spoofing,
fingerprinting, CAPTCHA solving or login. A blocked source is stopped and reported.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from scrapy import signals
from scrapy.downloadermiddlewares.robotstxt import RobotsTxtMiddleware
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Request, Response, TextResponse
from scrapy.http.request import NO_CALLBACK
from scrapy.utils.misc import build_from_crawler

from .sources import ROBOTS_TOKEN, Source

if TYPE_CHECKING:
    from scrapy.crawler import Crawler

    from .crawl_state import CrawlStore

logger = logging.getLogger(__name__)

MIN_DELAY_SECONDS = 3.0
DEFAULT_DELAY_SECONDS = 10.0
MAX_CRAWL_DELAY_SECONDS = 600.0  # a longer robots.txt crawl delay stops the source
MAX_RETRY_AFTER_SECONDS = 300.0  # a longer Retry-After stops the source for this run
DEFAULT_BACKOFF_SECONDS = 30.0
MAX_BACKOFF_RETRIES = 2
MAX_RATE_LIMITED = 3
MAX_FORBIDDEN = 2
TDMREP_TTL_SECONDS = 7 * 24 * 3600
POLICY_KINDS = {"robots", "tdmrep"}

# Interstitial pages of bot-protection services: never parsed, always a stop.
STRONG_CHALLENGE_MARKERS = (
    # Cloudflare's interstitial; normal pages only load /cdn-cgi/challenge-platform/scripts/jsd.
    ("cloudflare challenge", re.compile(rb"_cf_chl_opt|cf-chl-widget|cf_chl_prog")),
    (
        "cloudflare challenge",
        re.compile(rb"<title>\s*just a moment\.\.\.\s*</title>", re.IGNORECASE),
    ),
    ("akamai bot manager", re.compile(rb"bm-verify|sec-if-cpt-container|_sec/cp_challenge")),
    ("perimeterx captcha", re.compile(rb"px-captcha|_pxCaptcha")),
    ("datadome captcha", re.compile(rb"(?:geo|ct)\.captcha-delivery\.com")),
    ("imperva block page", re.compile(rb"Incapsula incident ID")),
    ("distil challenge", re.compile(rb"Pardon Our Interruption", re.IGNORECASE)),
)
# Wording that marks a block page when the status already says "blocked" or the
# page is too small to be a storefront. Product pages often embed reCAPTCHA or
# Turnstile for newsletter forms, so these alone never stop a source on a normal page.
WEAK_CHALLENGE_MARKERS = re.compile(
    rb"captcha|challenges\.cloudflare\.com|awswaf|_Incapsula_Resource|"
    rb"are you a (?:robot|human)|verify (?:that )?you are (?:a )?human|"
    rb"unusual traffic|access denied|request blocked|bot detected",
    re.IGNORECASE,
)
SMALL_PAGE_BYTES = 15_000


def challenge_marker(response: Response) -> str | None:
    """Name of the bot-challenge or CAPTCHA a response carries, or None."""
    if response.headers.get(b"cf-mitigated", b"").lower() == b"challenge":
        return "cloudflare challenge"
    body = response.body[:400_000] if response.body else b""
    for name, pattern in STRONG_CHALLENGE_MARKERS:
        if pattern.search(body):
            return name
    blocked = response.status in (401, 403, 429, 503)
    small = len(body) < SMALL_PAGE_BYTES and b"<html" in body[:2000].lower()
    if (blocked or small) and WEAK_CHALLENGE_MARKERS.search(body):
        return "captcha or block page"
    return None


def retry_after_seconds(value: bytes | str | None, now: float | None = None) -> float | None:
    """Seconds from a ``Retry-After`` header (delta-seconds or an HTTP date)."""
    if value is None:
        return None
    text = value.decode("latin-1") if isinstance(value, bytes) else value
    text = text.strip()
    if text.isdigit():
        return float(text)
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if moment is None:
        return None
    return max(0.0, moment.timestamp() - (now if now is not None else time.time()))


@dataclass
class SourceTelemetry:
    """Per-source counters that become the ``crawl`` section of the run report."""

    requests: int = 0
    statuses: Counter = field(default_factory=Counter)
    kinds: Counter = field(default_factory=Counter)
    bytes: int = 0
    not_modified: int = 0
    robots_disallowed: int = 0
    backoffs: int = 0
    forbidden: int = 0
    rate_limited: int = 0
    stopped: str | None = None
    budget_exhausted: bool = False
    errors: Counter = field(default_factory=Counter)
    crawl_delays: dict[str, float] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None

    def as_dict(self) -> dict[str, Any]:
        end = self.finished if self.finished is not None else time.monotonic()
        return {
            "requests": self.requests,
            "status_counts": {str(key): value for key, value in sorted(self.statuses.items())},
            "requests_by_kind": dict(self.kinds),
            "bytes": self.bytes,
            "not_modified": self.not_modified,
            "robots_disallowed": self.robots_disallowed,
            "backoffs": self.backoffs,
            "stopped": self.stopped,
            "budget_exhausted": self.budget_exhausted,
            "errors_by_reason": dict(self.errors),
            "crawl_delays": self.crawl_delays,
            "latency_ms_p50": _ms(percentile(self.latencies, 0.5)),
            "latency_ms_p95": _ms(percentile(self.latencies, 0.95)),
            "duration_seconds": round(end - self.started, 1),
        }


def _ms(seconds: float | None) -> int | None:
    return round(seconds * 1000) if seconds is not None else None


class Telemetry:
    """Shared crawl state the middlewares and the spider both read and update."""

    def __init__(
        self,
        sources: list[Source],
        *,
        max_requests: int = 200,
        min_delay: float = DEFAULT_DELAY_SECONDS,
        max_forbidden: int = MAX_FORBIDDEN,
        store: CrawlStore | None = None,
    ):
        self.sources = {source.key: source for source in sources}
        self.by_source = {source.key: SourceTelemetry() for source in sources}
        self.max_requests = max_requests
        self.min_delay = min_delay
        self.max_forbidden = max_forbidden
        self.store = store
        self.floors: dict[str, float] = {}
        self.backoff_until: dict[str, float] = {}
        self.last_sent: dict[str, float] = {}

    def source_for(self, request: Request) -> str | None:
        key = request.meta.get("source_key")
        if key in self.by_source:
            return key
        host = (urlsplit(request.url).hostname or "").lower()
        matches = [
            source
            for source in self.sources.values()
            if host == source.host or host.endswith("." + source.host)
        ]
        return max(matches, key=lambda source: len(source.host)).key if matches else None

    def get(self, source_key: str | None) -> SourceTelemetry | None:
        return self.by_source.get(source_key) if source_key else None

    def stop(self, source_key: str | None, reason: str) -> None:
        stats = self.get(source_key)
        if stats is not None and stats.stopped is None:
            stats.stopped = reason
            stats.errors[f"stopped: {reason}"] += 1
            logger.warning("Stopping source %s: %s", source_key, reason)

    def stopped(self, source_key: str | None) -> str | None:
        stats = self.get(source_key)
        return stats.stopped if stats is not None else None

    def delay_for(self, host: str) -> float:
        return max(self.min_delay, self.floors.get(host, 0.0))


def telemetry_of(crawler: Crawler) -> Telemetry | None:
    spider = getattr(crawler, "spider", None)
    return getattr(spider, "telemetry", None)


class _DisallowAll:
    def allowed(self, url: str | bytes, user_agent: str | bytes) -> bool:
        return False

    def crawl_delay(self, user_agent: str | bytes) -> float | None:
        return None


class _AllowAll:
    def allowed(self, url: str | bytes, user_agent: str | bytes) -> bool:
        return True

    def crawl_delay(self, user_agent: str | bytes) -> float | None:
        return None


def _location_matches(pattern: str, path: str) -> bool:
    pattern = pattern if pattern.startswith("/") else "/" + pattern
    return re.match(re.escape(pattern).replace(r"\*", ".*"), path) is not None


def tdm_reserved(rules: list[Any], url: str) -> bool:
    """TDMRep: the first rule whose location matches the path decides."""
    path = urlsplit(url).path or "/"
    for rule in rules:
        if isinstance(rule, dict) and _location_matches(str(rule.get("location", "")), path):
            return str(rule.get("tdm-reservation")) == "1"
    return False


class PoliteRobotsMiddleware(RobotsTxtMiddleware):
    """robots.txt with strict status handling, crawl delays and TDMRep checks."""

    def __init__(self, crawler: Crawler):
        super().__init__(crawler)
        self._robots_token = crawler.settings.get("ROBOTSTXT_USER_AGENT") or ROBOTS_TOKEN
        self._bodies: dict[str, bytes] = {}
        self.statuses: dict[str, int | str] = {}
        self._tdm: dict[str, list | None | asyncio.Future] = {}

    async def process_request(self, request: Request, spider: Any = None) -> Response | None:
        if request.meta.get("dont_obey_robotstxt"):
            return None
        if request.url.startswith(("data:", "file:")):
            return None
        rp = await self.robot_parser(request)
        netloc = urlsplit(request.url).netloc
        if request.meta.get("kind") == "robots-sitemaps":
            # The spider wants the Sitemap: lines; serve the copy already fetched.
            return TextResponse(
                request.url,
                status=200,
                body=self._bodies.get(netloc, b""),
                encoding="utf-8",
                request=request,
                flags=["robots-cache"],
            )
        self.process_request_2(rp, request)
        if request.meta.get("kind") not in {"sitemap", "robots", "tdmrep"}:
            rules = await self._tdm_rules(request)
            telemetry = telemetry_of(self.crawler)
            source = telemetry.source_for(request) if telemetry else None
            if rules is None:
                if telemetry:
                    telemetry.stop(source, f"unreadable TDMRep file on {netloc}")
                raise IgnoreRequest("unreadable TDMRep file")
            if tdm_reserved(rules, request.url):
                if telemetry:
                    telemetry.stop(source, "text-and-data-mining reserved (TDMRep)")
                raise IgnoreRequest("text-and-data-mining reserved (TDMRep)")
        return None

    def process_request_2(self, rp: Any, request: Request) -> None:
        if rp is None:
            return
        if not rp.allowed(request.url, self._robots_token):
            telemetry = telemetry_of(self.crawler)
            stats = telemetry.get(telemetry.source_for(request)) if telemetry else None
            if stats is not None:
                stats.robots_disallowed += 1
            self._stats.inc_value("robotstxt/forbidden")
            raise IgnoreRequest("Forbidden by robots.txt")

    async def robot_parser(self, request: Request) -> Any:
        """The host's parsed robots.txt, fetched once per host.

        Scrapy shares one Deferred between all requests that wait for robots.txt,
        but ``Deferred.asFuture`` hands the result to the first waiter only and
        ``None`` (meaning "allowed") to the rest. Each waiter here awaits the same
        asyncio future instead, so every request sees the real rules.
        """
        parts = urlsplit(request.url)
        netloc = parts.netloc
        if netloc not in self._parsers:
            future = asyncio.get_running_loop().create_future()
            self._parsers[netloc] = future
            robots_request = Request(
                f"{parts.scheme}://{netloc}/robots.txt",
                priority=self.DOWNLOAD_PRIORITY,
                meta={"dont_obey_robotstxt": True, "kind": "robots"},
                callback=NO_CALLBACK,
            )
            try:
                response = await self.crawler.engine.download_async(robots_request)
                rp = await self._interpret_robots(response, netloc, request)
            except Exception as exc:  # noqa: BLE001 - any failure means "disallow all"
                rp = self._robots_failure(exc, netloc, request)
            self._stats.inc_value("robotstxt/request_count")
            self._parsers[netloc] = rp
            future.set_result(rp)
        parser = self._parsers[netloc]
        if isinstance(parser, asyncio.Future):
            return await asyncio.shield(parser)
        return parser

    async def _interpret_robots(self, response: Response, netloc: str, request: Request) -> Any:
        self._stats.inc_value("robotstxt/response_count")
        self._stats.inc_value(f"robotstxt/response_status_count/{response.status}")
        telemetry = telemetry_of(self.crawler)
        source = telemetry.source_for(request) if telemetry else None
        status = response.status
        self.statuses[netloc] = status
        marker = challenge_marker(response)
        if marker:
            if telemetry:
                telemetry.stop(source, f"robots.txt answered with a {marker}")
            return _DisallowAll()
        if 200 <= status < 300:
            rp = build_from_crawler(self._parserimpl, self.crawler, response.body)
            self._bodies[netloc] = response.body
            self._apply_crawl_delay(rp, response, source)
            await self.crawler.signals.send_catch_log_async(
                signal=signals.robots_parsed, robotparser=rp, request=request
            )
            return rp
        if status in (401, 403):
            if telemetry:
                telemetry.stop(source, f"robots.txt HTTP {status}")
            return _DisallowAll()
        if status == 429 or status >= 500:
            if telemetry:
                telemetry.stop(source, f"robots.txt unreachable (HTTP {status})")
            return _DisallowAll()
        return _AllowAll()  # RFC 9309: an unavailable robots.txt (4xx) means no restrictions

    def _robots_failure(self, exc: Exception, netloc: str, request: Request) -> Any:
        # RFC 9309 section 2.3.1.4: an unreachable robots.txt means complete disallow.
        telemetry = telemetry_of(self.crawler)
        if telemetry is not None and not isinstance(exc, IgnoreRequest):
            telemetry.stop(
                telemetry.source_for(request),
                f"robots.txt could not be fetched ({type(exc).__name__})",
            )
        self.statuses[netloc] = type(exc).__name__
        return _DisallowAll()

    def _apply_crawl_delay(self, rp: Any, response: Response, source: str | None) -> None:
        telemetry = telemetry_of(self.crawler)
        if telemetry is None:
            return
        host = (urlsplit(response.url).hostname or "").lower()
        delay = rp.crawl_delay(self._robots_token) if hasattr(rp, "crawl_delay") else None
        protego = getattr(rp, "rp", None)
        rate = protego.request_rate(self._robots_token) if protego is not None else None
        if rate is not None and rate.requests:
            delay = max(delay or 0.0, rate.seconds / rate.requests)
        if delay is None:
            return
        stats = telemetry.get(source)
        if stats is not None:
            stats.crawl_delays[host] = delay
        if delay > MAX_CRAWL_DELAY_SECONDS:
            telemetry.stop(source, f"robots.txt crawl delay of {delay:g} s is too long")
        telemetry.floors[host] = max(telemetry.floors.get(host, 0.0), delay)

    async def _tdm_rules(self, request: Request) -> list | None:
        parts = urlsplit(request.url)
        netloc = parts.netloc
        if netloc not in self._tdm:
            future = asyncio.get_running_loop().create_future()
            self._tdm[netloc] = future
            rules = await self._fetch_tdm(f"{parts.scheme}://{netloc}", parts.hostname or "")
            self._tdm[netloc] = rules
            future.set_result(rules)
        value = self._tdm[netloc]
        if isinstance(value, asyncio.Future):
            return await asyncio.shield(value)
        return value

    async def _fetch_tdm(self, origin: str, host: str) -> list | None:
        telemetry = telemetry_of(self.crawler)
        store = telemetry.store if telemetry else None
        cached = store.host_policy(host) if store else None
        if cached is not None:
            from .crawl_state import instant

            fetched = instant(cached["fetched_at"])
            if fetched and time.time() - fetched.timestamp() < TDMREP_TTL_SECONDS:
                return json.loads(cached["tdmrep"])
        tdm_request = Request(
            origin + "/.well-known/tdmrep.json",
            meta={"dont_obey_robotstxt": True, "kind": "tdmrep"},
            callback=NO_CALLBACK,
        )
        try:
            response = await self.crawler.engine.download_async(tdm_request)
        except Exception:  # noqa: BLE001 - any failure means the rules are unreadable
            return None
        if 300 <= response.status < 500 and response.status not in (401, 403, 429):
            rules: list | None = []  # no file (a 3xx without a usable target counts too)
        elif 200 <= response.status < 300:
            try:
                parsed = json.loads(response.body)
            except ValueError:
                parsed = None
            rules = parsed if isinstance(parsed, list) else None
        else:
            rules = None
        if rules is not None and store is not None:
            store.save_host_policy(host, rules)
        return rules


class PoliteResponseMiddleware:
    """Stop on blocks and challenges, back off on rate limits, keep validators."""

    def __init__(self, crawler: Crawler):
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> PoliteResponseMiddleware:
        return cls(crawler)

    def process_response(
        self, request: Request, response: Response, spider: Any = None
    ) -> Request | Response:
        telemetry = telemetry_of(self.crawler)
        if telemetry is None or "robots-cache" in response.flags:
            return response
        source = telemetry.source_for(request)
        stats = telemetry.get(source)
        kind = request.meta.get("kind") or (
            "robots" if request.meta.get("dont_obey_robotstxt") else "page"
        )
        if kind in POLICY_KINDS:
            return response  # the robots middleware interprets policy files itself
        marker = challenge_marker(response)
        if marker:
            telemetry.stop(source, f"{marker} (HTTP {response.status})")
            raise IgnoreRequest(f"{marker} page")
        if response.headers.get(b"tdm-reservation", b"").strip() == b"1":
            telemetry.stop(source, "text-and-data-mining reserved (tdm-reservation header)")
            raise IgnoreRequest("text-and-data-mining reserved")
        if stats is None:
            return response
        if response.status in (401, 403):
            stats.forbidden += 1
            if stats.forbidden >= telemetry.max_forbidden:
                telemetry.stop(source, f"HTTP {response.status} answered {stats.forbidden} times")
            return response
        if response.status in (429, 503):
            return self._back_off(request, response, telemetry, source, stats)
        if response.status == 304:
            stats.not_modified += 1
        elif 200 <= response.status < 300 and telemetry.store is not None and source:
            telemetry.store.save_validators(
                request.url,
                source,
                etag=_header(response, b"ETag"),
                last_modified=_header(response, b"Last-Modified"),
                body=response.body,
                status=response.status,
            )
        return response

    def _back_off(
        self,
        request: Request,
        response: Response,
        telemetry: Telemetry,
        source: str | None,
        stats: SourceTelemetry,
    ) -> Request | Response:
        stats.rate_limited += 1
        retries = int(request.meta.get("backoff_retries", 0))
        wait = retry_after_seconds(response.headers.get(b"Retry-After"))
        if wait is None:
            wait = DEFAULT_BACKOFF_SECONDS * (2**retries)
        if wait > MAX_RETRY_AFTER_SECONDS:
            telemetry.stop(source, f"HTTP {response.status} with Retry-After {wait:.0f} s")
            return response
        if stats.rate_limited >= MAX_RATE_LIMITED:
            telemetry.stop(source, f"HTTP {response.status} answered {stats.rate_limited} times")
            return response
        if retries >= MAX_BACKOFF_RETRIES:
            return response
        host = (urlsplit(request.url).hostname or "").lower()
        telemetry.backoff_until[host] = max(
            telemetry.backoff_until.get(host, 0.0), time.monotonic() + wait
        )
        # Stay slower for the rest of the run: double this host's spacing (at most 60 s).
        telemetry.floors[host] = min(
            60.0, max(telemetry.delay_for(host) * 2, telemetry.floors.get(host, 0.0))
        )
        stats.backoffs += 1
        retry = request.replace(dont_filter=True)
        retry.meta["backoff_retries"] = retries + 1
        return retry


def _header(response: Response, name: bytes) -> str | None:
    value = response.headers.get(name)
    return value.decode("latin-1") if value else None


class PoliteGateMiddleware:
    """Per-host concurrency cap and request spacing, checked at the moment of sending."""

    def __init__(self, crawler: Crawler):
        self.crawler = crawler
        self._gates: dict[str, asyncio.Semaphore] = {}

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> PoliteGateMiddleware:
        return cls(crawler)

    def _gate(self, telemetry: Telemetry, host: str, source: str | None) -> asyncio.Semaphore:
        if host not in self._gates:
            configured = telemetry.sources.get(source) if source else None
            self._gates[host] = asyncio.Semaphore(configured.max_concurrency if configured else 1)
        return self._gates[host]

    @staticmethod
    def _check(telemetry: Telemetry, stats: SourceTelemetry | None, kind: str) -> None:
        if stats is None or kind in POLICY_KINDS:
            return
        if stats.stopped:
            raise IgnoreRequest(f"source stopped: {stats.stopped}")
        if stats.requests >= telemetry.max_requests:
            stats.budget_exhausted = True
            raise IgnoreRequest("request budget reached")

    async def process_request(self, request: Request, spider: Any = None) -> None:
        telemetry = telemetry_of(self.crawler)
        if telemetry is None:
            return
        host = (urlsplit(request.url).hostname or "").lower()
        source = telemetry.source_for(request)
        stats = telemetry.get(source)
        kind = request.meta.get("kind") or (
            "robots" if request.meta.get("dont_obey_robotstxt") else "page"
        )
        self._check(telemetry, stats, kind)
        gate = self._gate(telemetry, host, source)
        await gate.acquire()
        try:
            # Reserve the next start time before sleeping, so concurrent requests to
            # one host still start at least the required spacing apart.
            now = time.monotonic()
            start = max(
                now,
                telemetry.last_sent.get(host, -1e9) + telemetry.delay_for(host),
                telemetry.backoff_until.get(host, 0.0),
            )
            telemetry.last_sent[host] = start
            if start > now:
                await asyncio.sleep(start - now)
            self._check(telemetry, stats, kind)
            if request.meta.get("conditional") and telemetry.store is not None:
                validators = telemetry.store.validators(request.url)
                if validators and validators.get("etag"):
                    request.headers[b"If-None-Match"] = validators["etag"]
                if validators and validators.get("last_modified"):
                    request.headers[b"If-Modified-Since"] = validators["last_modified"]
            if stats is not None:
                stats.requests += 1
                stats.kinds[kind] += 1
        except BaseException:
            gate.release()
            raise
        request.meta["_gate_host"] = host

    def _release(self, request: Request) -> None:
        host = request.meta.pop("_gate_host", None)
        gate = self._gates.get(host) if host else None
        if gate is not None:
            gate.release()

    def process_response(
        self, request: Request, response: Response, spider: Any = None
    ) -> Response:
        self._release(request)
        if "robots-cache" in response.flags:
            return response
        telemetry = telemetry_of(self.crawler)
        stats = telemetry.get(telemetry.source_for(request)) if telemetry else None
        if stats is not None:
            stats.statuses[response.status] += 1
            stats.bytes += len(response.body or b"")
            latency = request.meta.get("download_latency")
            kind = request.meta.get("kind")
            if (
                isinstance(latency, int | float)
                and kind
                and kind not in POLICY_KINDS
                and len(stats.latencies) < 100_000
            ):
                stats.latencies.append(float(latency))
        return response

    def process_exception(self, request: Request, exception: Exception, spider: Any = None) -> None:
        self._release(request)
        telemetry = telemetry_of(self.crawler)
        stats = telemetry.get(telemetry.source_for(request)) if telemetry else None
        if stats is not None and not isinstance(exception, IgnoreRequest):
            stats.errors[f"network: {type(exception).__name__}"] += 1


def percentile(values: list[float], share: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(share * len(ordered)) - 1))]


def polite_settings(
    *,
    user_agent: str,
    min_delay: float = DEFAULT_DELAY_SECONDS,
    timeout: float = 20,
    autothrottle: bool = True,
    concurrency: int = 1,
) -> dict[str, Any]:
    """Scrapy settings for polite collection; callers add browser or test settings."""
    return {
        "BOT_NAME": "fashion_finder_data",
        "USER_AGENT": user_agent,
        "ROBOTSTXT_OBEY": True,
        "ROBOTSTXT_USER_AGENT": ROBOTS_TOKEN,
        "DOWNLOADER_MIDDLEWARES": {
            "scrapy.downloadermiddlewares.robotstxt.RobotsTxtMiddleware": None,
            "fashion_finder_data.politeness.PoliteRobotsMiddleware": 100,
            "fashion_finder_data.politeness.PoliteResponseMiddleware": 580,
            "fashion_finder_data.politeness.PoliteGateMiddleware": 950,
        },
        "DOWNLOAD_DELAY": min_delay,
        "DOWNLOAD_DELAY_JITTER": 0,
        "CONCURRENT_REQUESTS_PER_DOMAIN": concurrency,
        "AUTOTHROTTLE_ENABLED": autothrottle,
        "AUTOTHROTTLE_START_DELAY": min_delay,
        "AUTOTHROTTLE_MAX_DELAY": 60.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0,
        "DOWNLOAD_TIMEOUT": timeout,
        "DOWNLOAD_MAXSIZE": 512 * 1024 * 1024,
        "DOWNLOAD_WARNSIZE": 64 * 1024 * 1024,
        "RETRY_TIMES": 1,
        "RETRY_HTTP_CODES": [500, 502, 504, 522, 524, 408],
        "HTTPERROR_ALLOWED_CODES": [304, 404, 410],
        "REDIRECT_MAX_TIMES": 5,
        "COOKIES_ENABLED": False,
        "TELNETCONSOLE_ENABLED": False,
        "REMOTE_CONTROL_ENABLED": False,
        "LOG_LEVEL": "INFO",
        "DEFAULT_REQUEST_HEADERS": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.5",
        },
    }

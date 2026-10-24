"""Source definitions are validated config; new shops stay opt-in and unapproved."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fashion_finder_data.source_policy import load_policies
from fashion_finder_data.sources import (
    CRAWL_SOURCES,
    DEFAULT_COLLECT_SOURCES,
    SOURCES,
    contact_url,
    load_definitions,
    user_agent,
)

EXAMPLE_POLICIES = Path(__file__).parents[1] / "config" / "source-policies.example.json"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sources.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_collect_all_runs_only_reviewed_shops() -> None:
    assert set(DEFAULT_COLLECT_SOURCES) == {"reserved", "sinsay", "mohito", "house", "cropp"}
    for key in ("zara", "hm", "pullandbear", "sportano", "ecipo", "modivo"):
        assert key in CRAWL_SOURCES and key not in DEFAULT_COLLECT_SOURCES
    assert SOURCES["aboutyou"].crawl_enabled is False


def test_every_new_source_has_a_pending_policy() -> None:
    policies = load_policies(EXAMPLE_POLICIES)
    for key in ("sportano", "ecipo", "modivo"):
        policy = policies[(key, "HU")]
        assert policy.approval_status == "pending"
        assert not (policy.allow_public_listing or policy.allow_image_processing)


def test_a_shop_with_structured_data_is_configuration_only(tmp_path) -> None:
    sources = load_definitions(
        write(
            tmp_path,
            """
[sources.exampleshop]
name = "Example"
host = "example-shop.hu"
status = "candidate"
market_paths = ["/"]
product_pattern = '^https://example-shop\\.hu/termek/'
sitemaps = ["https://example-shop.hu/sitemap.xml"]
sitemap_follow = "product"
extraction = { type_aliases = { "Termék" = "Product" }, title_suffix = '\\s*-\\s*Example$' }
refresh = { interval_hours = 24 }
""",
        )
    )
    shop = sources["exampleshop"]
    assert shop.crawl_enabled and not shop.collect_by_default
    assert shop.interval_hours == 24 and shop.refresh_strategy == "product"


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ('host = "shop.hu"\nwhatever = 1', "unknown fields"),
        ('host = "shop.hu"\ncategory_url = "http://shop.hu/hu/c"', "https"),
        ('host = "shop.hu"\nsitemaps = ["https://evil.example/sitemap.xml"]', "own domain"),
        ('host = "shop.hu"\nmax_concurrency = 10', "max_concurrency"),
        ('host = "shop.hu"\nrefresh = { interval_hours = 0.1 }', "interval"),
        ('host = "shop.hu"\nproduct_pattern = "(["', "unterminated"),
    ],
)
def test_invalid_definitions_fail_loudly(tmp_path, snippet, message) -> None:
    with pytest.raises(Exception, match=message):
        load_definitions(write(tmp_path, f"[sources.shop]\n{snippet}\n"))


def test_user_agent_is_honest_and_contact_url_is_validated(monkeypatch) -> None:
    monkeypatch.setenv("FASHION_FINDER_CONTACT_URL", "https://fashion-finder.example/bot")
    assert user_agent() == "FashionFinderBot/0.1.0 (+https://fashion-finder.example/bot)"
    assert contact_url("mailto:owner@example.org") == "mailto:owner@example.org"
    for bad in ("http://plain.example", "javascript:alert(1)", "https://x.example/a (b)"):
        with pytest.raises(ValueError):
            user_agent(bad)
    assert json.loads(EXAMPLE_POLICIES.read_text())  # still valid JSON

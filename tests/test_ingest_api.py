"""The ingest API is token-only, bounded, idempotent and never publishes by itself."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from django.test import Client

from fashion_finder_data.api import ingest
from fashion_finder_data.api.views import api_context
from fashion_finder_data.catalogue.models import CollectionRun, PriceObservation, RetailerOffer
from fashion_finder_data.storage import open_database

TOKEN = "t" * 40
URL = "https://www.reserved.com/hu/hu/gyapju-kabat-101aa-99x"


def item(price=39995, *, sku="101AA-99X", url=URL, when=None) -> dict:
    when = when or datetime.now(UTC) - timedelta(minutes=5)
    return {
        "type": "observation",
        "record": {
            "source_key": "reserved",
            "source_url": url,
            "fetched_at": when.isoformat(),
            "product": {
                "url": url,
                "sku": sku,
                "name": "Gyapjú kabát",
                "price": price,
                "currency": "HUF",
                "availability": "in_stock",
            },
        },
    }


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv(ingest.TOKEN_ENV, TOKEN)
    ingest._calls.clear()
    database = open_database(tmp_path / "catalog.sqlite")
    with api_context(database):
        yield database, Client(enforce_csrf_checks=True)


def post(client: Client, body: dict | bytes, token: str = TOKEN):
    data = body if isinstance(body, bytes) else json.dumps(body)
    return client.post(
        "/api/v1/ingest/offers",
        data=data,
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )


def test_without_a_configured_token_every_endpoint_is_unavailable(tmp_path, monkeypatch) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    for value in (None, "too-short"):
        if value is None:
            monkeypatch.delenv(ingest.TOKEN_ENV, raising=False)
        else:
            monkeypatch.setenv(ingest.TOKEN_ENV, value)
        with api_context(database):
            client = Client()
            assert client.get("/api/v1/ingest/due?source=reserved").status_code == 503
            response = client.post(
                "/api/v1/ingest/offers",
                data="{}",
                content_type="application/json",
                HTTP_AUTHORIZATION="Bearer too-short",
            )
            assert response.status_code == 503


def test_missing_or_wrong_token_is_refused(api) -> None:
    _, client = api
    assert client.get("/api/v1/ingest/due?source=reserved").status_code == 401
    wrong = client.get(
        "/api/v1/ingest/due?source=reserved", HTTP_AUTHORIZATION="Bearer " + "x" * 40
    )
    assert wrong.status_code == 401
    assert wrong["WWW-Authenticate"] == "Bearer"
    assert post(client, {"items": [item()]}, token="nope").status_code == 401
    assert RetailerOffer.objects.using(api[0].alias).count() == 0


def test_oversize_bodies_and_batches_are_rejected(api) -> None:
    _, client = api
    huge = b'{"items": [], "pad": "' + b"x" * (ingest.MAX_BODY_BYTES + 10) + b'"}'
    assert post(client, huge).status_code == 413
    assert post(client, {"items": [item()] * 501}).status_code == 413
    assert post(client, b"not json").status_code == 400


def test_partial_batch_returns_per_item_results(api) -> None:
    database, client = api
    response = post(
        client,
        {
            "run_id": "gha-1",
            "items": [
                item(),
                item(price="ingyen", sku="BAD-1", url=URL.replace("101aa", "202bb")),
                item(url="https://www.reserved.com/pl/pl/kurtka-303cc-01x", sku="PL-1"),
                {"type": "failure", "offer_id": 12345, "source_key": "reserved"},
            ],
        },
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert [result["status"] for result in results] == [
        "created",
        "rejected",
        "rejected",
        "rejected",
    ]
    assert "price" in results[1]["reason"]
    assert RetailerOffer.objects.using(database.alias).count() == 1
    audit = CollectionRun.objects.using(database.alias).get(adapter_version="api-ingest-v1")
    details = json.loads(audit.completeness)
    assert details["actor"].startswith("ingest-token:") and TOKEN not in audit.completeness
    assert details["client_run_id"] == "gha-1"
    assert (audit.records_valid, audit.records_rejected) == (1, 3)


def test_demo_and_practice_rows_can_never_be_ingested_into_a_catalogue(api) -> None:
    database, client = api
    practice = item(url="https://practice.invalid/products/p-1", sku="P-1")
    practice["record"]["source_key"] = "practice"
    demo = item(url="https://fashion-finder-demo.invalid/products/d-1", sku="D-1")
    demo["record"]["source_key"] = "demo"

    response = post(client, {"items": [practice, demo]})

    assert response.status_code == 200
    results = response.json()["results"]
    assert [result["status"] for result in results] == ["rejected", "rejected"]
    assert all("not a retailer source" in result["reason"] for result in results)
    assert RetailerOffer.objects.using(database.alias).count() == 0
    for source in ("practice", "demo"):
        response = client.get(
            f"/api/v1/ingest/due?source={source}", HTTP_AUTHORIZATION=f"Bearer {TOKEN}"
        )
        assert response.status_code == 400


def test_replaying_a_batch_is_idempotent_and_unchanged_offers_are_not_rewritten(api) -> None:
    database, client = api
    batch = {"items": [item()]}
    assert post(client, batch).json()["counts"] == {"created": 1}
    first = RetailerOffer.objects.using(database.alias).get()
    replay = post(client, batch).json()
    later = post(client, {"items": [item(when=datetime.now(UTC))]}).json()
    assert replay["counts"] == {"unchanged": 1}
    assert later["counts"] == {"unchanged": 1}
    offer = RetailerOffer.objects.using(database.alias).get()
    assert offer.updated_at == first.updated_at
    assert offer.last_checked_at > first.last_checked_at
    assert PriceObservation.objects.using(database.alias).count() == 1


def test_due_endpoint_lists_overdue_offers_with_their_check_url(api) -> None:
    database, client = api
    post(client, {"items": [item()]})
    RetailerOffer.objects.using(database.alias).update(
        next_refresh_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    answer = client.get(
        "/api/v1/ingest/due?source=reserved&limit=10", HTTP_AUTHORIZATION=f"Bearer {TOKEN}"
    ).json()
    [due] = answer["offers"]
    assert (due["check_url"], due["check_kind"], due["price_amount"]) == (URL, "product", "39995")
    assert answer["stats"]["due_now"] == 1
    assert (
        client.get(
            "/api/v1/ingest/due?source=unknown", HTTP_AUTHORIZATION=f"Bearer {TOKEN}"
        ).status_code
        == 400
    )
    assert (
        client.get("/api/v1/ingest/offers", HTTP_AUTHORIZATION=f"Bearer {TOKEN}").status_code == 405
    )


def test_ingest_never_publishes_without_an_approved_policy(api) -> None:
    _, client = api
    post(client, {"items": [item()]})
    assert client.get("/api/v1/search").json()["items"] == []


def test_requests_beyond_the_rate_limit_get_429(api, monkeypatch) -> None:
    _, client = api
    monkeypatch.setitem(ingest.RATE_LIMITS, "GET", (2, 60.0))
    headers = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}
    codes = [
        client.get("/api/v1/ingest/due?source=reserved", **headers).status_code for _ in range(3)
    ]
    assert codes == [200, 200, 429]

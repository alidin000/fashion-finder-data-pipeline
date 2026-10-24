"""Due-queue refresh rules: skip unchanged writes, back off, confirm removals, stretch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fashion_finder_data.catalogue.models import PriceObservation, RetailerOffer
from fashion_finder_data.scheduler import (
    apply_batch,
    backoff,
    due_offers,
    plan_capacity,
    queue_stats,
)
from fashion_finder_data.storage import open_database

T0 = datetime(2026, 10, 24, 8, tzinfo=UTC)
LISTING = "https://www.reserved.com/hu/hu/noi/ruhazat/kabatok"
COAT = "https://www.reserved.com/hu/hu/gyapju-kabat-101aa-99x"


def observed(when: datetime, price: int = 39995, *, page: str = LISTING, **product) -> dict:
    return {
        "type": "observation",
        "record": {
            "source_key": "reserved",
            "source_url": page,
            "fetched_at": when.isoformat(),
            "product": {
                "url": COAT,
                "sku": "101AA-99X",
                "name": "Gyapjú kabát",
                "price": price,
                "currency": "HUF",
                "sizes": ["S", "M"],
                "availability": "in_stock",
                **product,
            },
        },
    }


def failure(offer_id: int, when: datetime, reason: str = "HTTP 503", *, gone: bool = False):
    return {
        "type": "failure",
        "offer_id": offer_id,
        "source_key": "reserved",
        "checked_at": when.isoformat(),
        "reason": reason,
        "gone": gone,
    }


def stored(database) -> RetailerOffer:
    return RetailerOffer.objects.using(database.alias).get()


def test_new_offers_are_staggered_and_point_refresh_at_their_listing(tmp_path) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    [result] = apply_batch(database, [observed(T0)], now=T0)
    offer = stored(database)
    assert result["status"] == "created"
    assert offer.refresh_url == LISTING
    assert offer.last_checked_at == T0
    assert T0 + timedelta(hours=6) <= offer.next_refresh_at < T0 + timedelta(hours=12)


def test_unchanged_check_writes_only_check_times(tmp_path) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    apply_batch(database, [observed(T0)], now=T0)
    before = stored(database)
    later = T0 + timedelta(hours=12)
    [result] = apply_batch(database, [observed(later)], now=later)
    after = stored(database)
    assert result["status"] == "unchanged"
    assert after.updated_at == before.updated_at  # no full-row write
    assert after.last_checked_at == later
    assert after.source_fetched_at == later.isoformat()  # still fresh for shoppers
    assert after.next_refresh_at == later + timedelta(hours=12)
    assert PriceObservation.objects.using(database.alias).count() == 1


def test_price_change_updates_offer_and_history(tmp_path) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    apply_batch(database, [observed(T0)], now=T0)
    later = T0 + timedelta(hours=12)
    [result] = apply_batch(database, [observed(later, 29995)], now=later)
    assert result["status"] == "updated"
    assert stored(database).price_amount == "29995"
    assert PriceObservation.objects.using(database.alias).count() == 2


def test_failures_back_off_exponentially_to_a_cap(tmp_path) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    apply_batch(database, [observed(T0)], now=T0)
    offer_id = stored(database).pk
    when = T0
    for expected in (30, 60, 120, 240):
        when += timedelta(hours=1)
        apply_batch(database, [failure(offer_id, when)], now=when)
        offer = stored(database)
        assert offer.next_refresh_at == when + timedelta(minutes=expected)
    assert offer.failure_count == 4 and offer.last_error == "HTTP 503"
    assert backoff(20) == timedelta(hours=24)
    apply_batch(database, [observed(when)], now=when)
    assert stored(database).failure_count == 0  # success clears the streak


def test_removal_needs_two_answers_six_hours_apart(tmp_path) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    apply_batch(database, [observed(T0)], now=T0)
    offer_id = stored(database).pk
    first = T0 + timedelta(hours=12)
    [result] = apply_batch(database, [failure(offer_id, first, "HTTP 404", gone=True)], now=first)
    assert result["detail"] == "removal pending confirmation"
    assert stored(database).status == "active"
    too_soon = first + timedelta(hours=1)
    apply_batch(database, [failure(offer_id, too_soon, "HTTP 404", gone=True)], now=too_soon)
    assert stored(database).status == "active"  # two answers, but only an hour apart
    confirmed = first + timedelta(hours=7)
    [result] = apply_batch(
        database, [failure(offer_id, confirmed, "HTTP 410", gone=True)], now=confirmed
    )
    offer = stored(database)
    assert result["detail"] == "marked unavailable"
    assert offer.status == "unavailable"
    assert offer.next_refresh_at == confirmed + timedelta(days=7)
    # It comes back as soon as a check finds it again.
    back = confirmed + timedelta(days=7)
    [result] = apply_batch(database, [observed(back)], now=back)
    assert result["status"] == "updated" and stored(database).status == "active"


def test_missing_from_listing_schedules_a_product_page_check(tmp_path) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    apply_batch(database, [observed(T0)], now=T0)
    offer_id = stored(database).pk
    missing = {
        "type": "missing",
        "offer_id": offer_id,
        "source_key": "reserved",
        "checked_at": T0.isoformat(),
        "listing_url": LISTING,
    }
    apply_batch(database, [missing], now=T0)
    due = due_offers(database, "reserved", limit=10, now=T0 + timedelta(hours=7))
    assert [(item["check_kind"], item["check_url"]) for item in due] == [("product", COAT)]
    # A product-page observation clears the flag and the stale listing link.
    later = T0 + timedelta(hours=7)
    apply_batch(database, [observed(later, page=COAT)], now=later)
    offer = stored(database)
    assert offer.last_error is None and offer.refresh_url is None


def test_due_queue_is_oldest_first_and_counts_stale_offers(tmp_path) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    apply_batch(database, [observed(T0)], now=T0)
    stats = queue_stats(database, "reserved", now=T0 + timedelta(hours=19))
    assert (stats["active_offers"], stats["due_now"], stats["stale_over_1_5x_interval"]) == (
        1,
        1,
        1,
    )
    assert due_offers(database, "reserved", limit=5, now=T0) == []


def test_capacity_stretches_the_interval_instead_of_breaking_crawl_delay() -> None:
    tight = plan_capacity("reserved", offers=12_000, units=12_000, spacing_seconds=10)
    assert tight.stretched and tight.effective_interval_hours == round(12_000 * 10 / 0.8 / 3600, 2)
    listing = plan_capacity(
        "reserved", offers=12_000, units=28, spacing_seconds=10, requests_per_unit=2
    )
    assert not listing.stretched and listing.effective_interval_hours == 12.0


def test_invalid_items_are_rejected_without_sinking_the_batch(tmp_path) -> None:
    database = open_database(tmp_path / "catalog.sqlite")
    future = T0 + timedelta(days=1)
    results = apply_batch(
        database,
        [
            observed(T0),
            observed(future),
            {"type": "observation", "record": {"source_key": "nope"}},
            failure(999, T0),
            {"type": "teleport"},
        ],
        now=T0,
    )
    assert [result["status"] for result in results] == [
        "created",
        "rejected",
        "rejected",
        "rejected",
        "rejected",
    ]
    assert results[1]["reason"] == "observation time is in the future"
    assert results[3]["reason"] == "unknown offer for this source"

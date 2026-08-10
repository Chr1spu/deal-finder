"""The watchlist: one listing followed over time.

The deal feed is rebuilt from scratch by every scan, so a listing that stops
being a bargain leaves it and takes its history with it. These tests cover the
things that only matter because a watchlist row outlives the state that
created it: the price it was added at stays frozen, and a listing that ends is
kept rather than dropped, because "it sold" is the outcome being recorded.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from api.db import get_session
from api.main import app
from api.models import Listing, ListingStatus, PriceObservation, WatchlistItem


@pytest.fixture()
def client(test_engine, monkeypatch, api_key):
    import api.routes.watchlist as routes

    monkeypatch.setattr(routes, "engine", test_engine)

    def override_get_session():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        test_client.headers.update({"X-API-Key": api_key})
        yield test_client
    app.dependency_overrides.clear()


def seed_listing(test_engine, price: float = 500.0, **kwargs) -> int:
    with Session(test_engine) as session:
        listing = Listing(
            source="ebay",
            source_id=kwargs.pop("source_id", "v1|123|0"),
            title=kwargs.pop("title", "ASUS TUF GeForce RTX 4090 24GB"),
            price=price,
            url="https://www.ebay.com/itm/123",
            **kwargs,
        )
        session.add(listing)
        session.commit()
        session.refresh(listing)
        return listing.id or 0


def test_adding_a_listing_freezes_the_price_it_was_added_at(client, test_engine):
    listing_id = seed_listing(test_engine, price=500.0)

    response = client.post("/watchlist", json={"listing_id": listing_id, "note": "watch this"})
    assert response.status_code == 201
    assert response.json()["price_when_added"] == 500.0

    # The listing moves under the row, which is the whole reason the column
    # exists rather than being read off Listing.price at display time.
    with Session(test_engine) as session:
        listing = session.get(Listing, listing_id)
        listing.price = 400.0
        session.add(listing)
        session.commit()

    item = client.get("/watchlist").json()["items"][0]
    assert item["price_when_added"] == 500.0
    assert item["current_price"] == 400.0
    assert item["price_change_since_added"] == pytest.approx(-0.2)


def test_a_price_rise_reads_positive(client, test_engine):
    listing_id = seed_listing(test_engine, price=100.0)
    client.post("/watchlist", json={"listing_id": listing_id})

    with Session(test_engine) as session:
        listing = session.get(Listing, listing_id)
        listing.price = 150.0
        session.add(listing)
        session.commit()

    item = client.get("/watchlist").json()["items"][0]
    assert item["price_change_since_added"] == pytest.approx(0.5)


def test_watching_the_same_listing_twice_is_refused_rather_than_resetting_it(
    client, test_engine
):
    """Re-adding would silently reset the frozen price, which makes the price
    change wrong rather than absent. Wrong is worse."""
    listing_id = seed_listing(test_engine, price=500.0)
    assert client.post("/watchlist", json={"listing_id": listing_id}).status_code == 201

    with Session(test_engine) as session:
        listing = session.get(Listing, listing_id)
        listing.price = 300.0
        session.add(listing)
        session.commit()

    conflict = client.post("/watchlist", json={"listing_id": listing_id})
    assert conflict.status_code == 409
    assert "already on the watchlist" in conflict.json()["detail"]

    with Session(test_engine) as session:
        item = session.exec(select(WatchlistItem)).one()
        assert item.price_when_added == 500.0


def test_watching_a_listing_that_does_not_exist_is_a_404(client):
    assert client.post("/watchlist", json={"listing_id": 999999}).status_code == 404


def test_an_ended_listing_stays_on_the_watchlist_with_its_sale_confidence(
    client, test_engine
):
    """A listing that sold is the outcome the watchlist was recording."""
    missing_at = datetime.now(UTC) - timedelta(days=1)
    listing_id = seed_listing(
        test_engine,
        price=500.0,
        status=ListingStatus.likely_sold,
        missing_since=missing_at,
        sale_confidence=0.75,
        price_confidence=0.675,
    )
    client.post("/watchlist", json={"listing_id": listing_id})

    feed = client.get("/watchlist").json()
    assert feed["ended_count"] == 1
    assert feed["active_count"] == 0
    item = feed["items"][0]
    assert item["status"] == "likely_sold"
    assert item["sale_confidence"] == 0.75
    assert item["price_confidence"] == 0.675

    # Hideable, but not by default.
    hidden = client.get("/watchlist", params={"include_ended": False}).json()
    assert hidden["items"] == []
    assert hidden["ended_count"] == 1


def test_price_history_comes_back_oldest_first(client, test_engine):
    listing_id = seed_listing(test_engine, price=450.0)
    base = datetime.now(UTC) - timedelta(days=3)
    with Session(test_engine) as session:
        for offset, price in enumerate((500.0, 475.0, 450.0)):
            session.add(
                PriceObservation(
                    listing_id=listing_id,
                    price=price,
                    observed_at=base + timedelta(days=offset),
                )
            )
        session.commit()

    client.post("/watchlist", json={"listing_id": listing_id})
    history = client.get("/watchlist").json()["items"][0]["history"]
    assert [point["price"] for point in history] == [500.0, 475.0, 450.0]


def test_the_note_can_be_changed_without_disturbing_the_frozen_price(client, test_engine):
    listing_id = seed_listing(test_engine, price=500.0)
    client.post("/watchlist", json={"listing_id": listing_id, "note": "first"})

    updated = client.patch(f"/watchlist/{listing_id}", json={"note": "second"})
    assert updated.status_code == 200
    assert updated.json()["note"] == "second"
    assert updated.json()["price_when_added"] == 500.0


def test_removing_a_listing_leaves_the_listing_itself_alone(client, test_engine):
    """Deleting the listing would delete comp data, which is never the intent
    of unwatching something."""
    listing_id = seed_listing(test_engine)
    client.post("/watchlist", json={"listing_id": listing_id})

    assert client.delete(f"/watchlist/{listing_id}").status_code == 204
    assert client.get("/watchlist").json()["items"] == []
    with Session(test_engine) as session:
        assert session.get(Listing, listing_id) is not None


def test_unwatching_something_never_watched_is_a_404(client, test_engine):
    listing_id = seed_listing(test_engine)
    assert client.delete(f"/watchlist/{listing_id}").status_code == 404


def test_an_empty_watchlist_is_an_empty_feed_not_an_error(client):
    feed = client.get("/watchlist").json()
    assert feed == {
        "items": [],
        "active_count": 0,
        "ended_count": 0,
        "note": feed["note"],
    }


def test_writes_require_the_api_key(client, test_engine):
    """Router-level dependency, so a new write endpoint is covered by default.
    tests/test_auth.py owns the rest of this behaviour."""
    listing_id = seed_listing(test_engine)
    no_key = {"X-API-Key": ""}
    assert client.post("/watchlist", json={"listing_id": listing_id}, headers=no_key).status_code == 401
    assert client.delete(f"/watchlist/{listing_id}", headers=no_key).status_code == 401
    assert client.patch(f"/watchlist/{listing_id}", json={"note": "x"}, headers=no_key).status_code == 401
    # The read stays open, like every other read in this API.
    assert client.get("/watchlist", headers=no_key).status_code == 200

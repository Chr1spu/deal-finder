"""GET /listings.

The route previously returned every column of every row with no pagination,
which was survivable at a few hundred listings and stopped being so at ten
thousand plus a 512-float vector each. These tests pin the shape of the fix.
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from api.db import get_session
from api.main import app
from api.models import EMBEDDING_DIM, Listing, ListingStatus


def make_listing(n: int, source: str = "ebay", status=ListingStatus.active) -> Listing:
    return Listing(
        source=source,
        source_id=f"v1|{n}|0",
        title=f"Listing {n}",
        price=100.0 + n,
        shipping_cost=10.0,
        url=f"https://www.ebay.com/itm/{n}",
        images=["https://i.ebayimg.com/images/g/x/s-l225.jpg"],
        status=status,
        # The column that made pagination urgent.
        embedding=[0.1] * EMBEDDING_DIM,
        sale_signals={"relisted": True},
    )


@pytest.fixture()
def client(test_engine):
    """Overrides the app's Session dependency with the throwaway SQLite one,
    so the route is exercised without touching the real database."""

    def override_get_session():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def seed(test_engine, count: int, **kwargs) -> None:
    with Session(test_engine) as session:
        for n in range(count):
            session.add(make_listing(n, **kwargs))
        session.commit()


def test_returns_listings(client, test_engine):
    seed(test_engine, 3)

    response = client.get("/listings")

    assert response.status_code == 200
    assert len(response.json()) == 3


def test_the_embedding_is_not_serialized(client, test_engine):
    """512 floats per row is a lot of payload for something no client can use,
    and psycopg hands back a numpy array where list[float] is annotated, which
    would fail validation anyway."""
    seed(test_engine, 1)

    payload = client.get("/listings").json()[0]

    assert "embedding" not in payload
    assert "sale_signals" not in payload, "debugging detail, not feed content"
    assert "price" in payload, "sanity: the schema isn't just empty"


def test_total_cost_is_exposed_alongside_price(client, test_engine):
    """price is item-only. total_cost is what a comparison should use, so it
    ships with the response rather than being left for each client to
    recompute and get wrong."""
    seed(test_engine, 1)

    payload = client.get("/listings").json()[0]

    assert payload["price"] == 100.0
    assert payload["shipping_cost"] == 10.0
    assert payload["total_cost"] == 110.0


def test_limit_and_offset_page_through_results(client, test_engine):
    seed(test_engine, 5)

    first = client.get("/listings", params={"limit": 2}).json()
    second = client.get("/listings", params={"limit": 2, "offset": 2}).json()

    assert len(first) == 2
    assert len(second) == 2
    assert {row["source_id"] for row in first}.isdisjoint({row["source_id"] for row in second})


def test_limit_is_capped(client, test_engine):
    """An unbounded limit would just reintroduce the original problem."""
    seed(test_engine, 1)

    assert client.get("/listings", params={"limit": 10_000}).status_code == 422
    assert client.get("/listings", params={"limit": 0}).status_code == 422


def test_default_limit_applies_without_being_asked(client, test_engine):
    """The bug was that the route returned everything by default. Explicitly
    pinned so nobody quietly removes the default later."""
    from api.routes.listings import DEFAULT_LIMIT

    seed(test_engine, DEFAULT_LIMIT + 10)

    assert len(client.get("/listings").json()) == DEFAULT_LIMIT


def test_filters_by_source_and_status(client, test_engine):
    with Session(test_engine) as session:
        session.add(make_listing(1, source="ebay"))
        session.add(make_listing(2, source="depop"))
        session.add(make_listing(3, source="ebay", status=ListingStatus.likely_sold))
        session.commit()

    by_source = client.get("/listings", params={"source": "depop"}).json()
    assert [row["source"] for row in by_source] == ["depop"]

    by_status = client.get("/listings", params={"status": "likely_sold"}).json()
    assert [row["status"] for row in by_status] == ["likely_sold"]


# ------------------------------------------------------- price history


def test_price_history_is_oldest_first(client, test_engine):
    from datetime import datetime, timezone

    from api.models import PriceObservation

    with Session(test_engine) as session:
        listing = make_listing(1)
        session.add(listing)
        session.commit()
        session.refresh(listing)
        for day, price in ((3, 300.0), (1, 100.0), (2, 200.0)):
            session.add(
                PriceObservation(
                    listing_id=listing.id,
                    price=price,
                    shipping_cost=10.0,
                    observed_at=datetime(2026, 8, day, tzinfo=timezone.utc),
                )
            )
        session.commit()
        listing_id = listing.id

    points = client.get(f"/listings/{listing_id}/prices").json()

    assert [p["price"] for p in points] == [100.0, 200.0, 300.0]
    assert points[0]["total_cost"] == 110.0


def test_unknown_shipping_leaves_total_cost_null_rather_than_equal_to_price(client, test_engine):
    """Charting unknown shipping as zero would draw a delivered-cost line that
    never existed, and it biases in the dangerous direction."""
    from datetime import datetime, timezone

    from api.models import PriceObservation

    with Session(test_engine) as session:
        listing = make_listing(2)
        session.add(listing)
        session.commit()
        session.refresh(listing)
        session.add(
            PriceObservation(
                listing_id=listing.id,
                price=50.0,
                shipping_cost=None,
                observed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
        )
        session.commit()
        listing_id = listing.id

    point = client.get(f"/listings/{listing_id}/prices").json()[0]

    assert point["shipping_cost"] is None
    assert point["total_cost"] is None


def test_a_listing_with_no_observations_returns_an_empty_list(client, test_engine):
    seed(test_engine, 1)
    with Session(test_engine) as session:
        listing_id = session.exec(select(Listing)).first().id
    assert client.get(f"/listings/{listing_id}/prices").json() == []

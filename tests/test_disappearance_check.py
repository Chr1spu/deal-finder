from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from api.models import Listing, ListingStatus
from connectors.disappearance_check import check_all_sources, check_ebay_listings


class FakeEbayClient:
    """get_item returns None for IDs in `gone`, a dummy payload otherwise.
    Mirrors the real client's 404-means-sold contract without any network."""

    def __init__(self, gone: set[str]):
        self._gone = gone

    def get_item(self, item_id: str) -> dict | None:
        return None if item_id in self._gone else {"itemId": item_id}


def seed_listing(
    session: Session, source_id: str, source: str = "ebay", status: ListingStatus = ListingStatus.active
) -> None:
    session.add(
        Listing(
            source=source,
            source_id=source_id,
            title=f"item {source_id}",
            price=10.0,
            url=f"https://ebay.com/{source_id}",
            status=status,
            last_seen_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    session.commit()


def test_vanished_listing_is_marked_likely_sold(test_engine):
    with Session(test_engine) as session:
        seed_listing(session, "sold-1")

    checked, marked_sold = check_ebay_listings(client=FakeEbayClient(gone={"sold-1"}), db_engine=test_engine)

    assert (checked, marked_sold) == (1, 1)
    with Session(test_engine) as session:
        listing = session.exec(select(Listing).where(Listing.source_id == "sold-1")).one()
        assert listing.status == ListingStatus.likely_sold


def test_still_active_listing_keeps_status_and_bumps_last_seen(test_engine):
    with Session(test_engine) as session:
        seed_listing(session, "still-here")
        original_last_seen = session.exec(select(Listing)).one().last_seen_at

    checked, marked_sold = check_ebay_listings(client=FakeEbayClient(gone=set()), db_engine=test_engine)

    assert (checked, marked_sold) == (1, 0)
    with Session(test_engine) as session:
        listing = session.exec(select(Listing).where(Listing.source_id == "still-here")).one()
        assert listing.status == ListingStatus.active
        assert listing.last_seen_at > original_last_seen


def test_only_checks_active_listings_not_already_sold_ones(test_engine):
    with Session(test_engine) as session:
        seed_listing(session, "already-sold", status=ListingStatus.likely_sold)

    checked, marked_sold = check_ebay_listings(client=FakeEbayClient(gone=set()), db_engine=test_engine)

    assert (checked, marked_sold) == (0, 0)


def test_check_all_sources_loops_every_registered_source(test_engine, monkeypatch):
    """PULL_BASED_SOURCES only has "ebay" registered today (Depop doesn't
    exist yet), so fake out a second source to prove the loop itself works
    per-source rather than being hardcoded to eBay."""

    class FakeGoneClient:
        def get_item(self, item_id: str) -> dict | None:
            return None

    class FakeStillActiveClient:
        def get_item(self, item_id: str) -> dict | None:
            return {"id": item_id}

    monkeypatch.setattr(
        "connectors.disappearance_check.PULL_BASED_SOURCES",
        {"ebay": FakeGoneClient, "depop": FakeStillActiveClient},
    )

    with Session(test_engine) as session:
        seed_listing(session, "ebay-1", source="ebay")
        seed_listing(session, "depop-1", source="depop")

    checked, marked_sold = check_all_sources(db_engine=test_engine)

    assert (checked, marked_sold) == (2, 1)
    with Session(test_engine) as session:
        ebay_listing = session.exec(select(Listing).where(Listing.source_id == "ebay-1")).one()
        depop_listing = session.exec(select(Listing).where(Listing.source_id == "depop-1")).one()
        assert ebay_listing.status == ListingStatus.likely_sold
        assert depop_listing.status == ListingStatus.active

"""Disappearance tracking: re-check active listings, mark ones that vanish
as likely_sold. This is how sold-price history gets built (see
PROJECT_PLAN.md section 1). Start running it early so data accumulates.

Generalized per source (stage 2, per docs/decisions/0001): each pull-based
source gets an entry in PULL_BASED_SOURCES, check_all_sources loops them
all. Push-based sources (Facebook) aren't checked here at all, see the ADR.

Run manually for now (`python -m connectors.disappearance_check`); wired
into the RQ scheduler via systems/queue.py + systems/scheduler.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import Engine
from sqlmodel import Session, select

from api.db import engine as default_engine
from api.models import Listing, ListingStatus
from connectors.ebay import EbayClient


class SourceClient(Protocol):
    def get_item(self, item_id: str) -> dict | None: ...


PULL_BASED_SOURCES: dict[str, type[SourceClient]] = {
    "ebay": EbayClient,
}


def check_listings_for_source(
    source: str, client: SourceClient | None = None, db_engine: Engine | None = None
) -> tuple[int, int]:
    """client/db_engine are injectable so tests can run against a fake client
    and a throwaway DB instead of hitting the real source or Postgres."""
    client = client or PULL_BASED_SOURCES[source]()
    db_engine = db_engine or default_engine
    checked = 0
    marked_sold = 0

    with Session(db_engine) as session:
        active = session.exec(
            select(Listing).where(Listing.source == source, Listing.status == ListingStatus.active)
        ).all()

        for listing in active:
            checked += 1
            if client.get_item(listing.source_id) is None:
                listing.status = ListingStatus.likely_sold
                marked_sold += 1
            else:
                listing.last_seen_at = datetime.now(timezone.utc)
            session.add(listing)

        session.commit()

    return checked, marked_sold


def check_ebay_listings(client: EbayClient | None = None, db_engine: Engine | None = None) -> tuple[int, int]:
    """Thin backward-compatible wrapper around check_listings_for_source("ebay", ...)."""
    return check_listings_for_source("ebay", client=client, db_engine=db_engine)


def check_all_sources(db_engine: Engine | None = None) -> tuple[int, int]:
    """Runs check_listings_for_source for every registered pull-based source."""
    total_checked = 0
    total_marked_sold = 0
    for source in PULL_BASED_SOURCES:
        checked, marked_sold = check_listings_for_source(source, db_engine=db_engine)
        total_checked += checked
        total_marked_sold += marked_sold
    return total_checked, total_marked_sold


if __name__ == "__main__":
    checked, marked_sold = check_all_sources()
    print(f"Checked {checked} listings, marked {marked_sold} as likely_sold")

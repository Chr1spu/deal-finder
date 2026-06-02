"""Disappearance tracking: re-check active listings, mark ones that vanish
as likely_sold. This is how sold-price history gets built (see
PROJECT_PLAN.md section 1). Start running it early so data accumulates.

Run manually / via cron for now (`python -m connectors.disappearance_check`);
moves onto the RQ scheduler in stage 2.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Engine
from sqlmodel import Session, select

from api.db import engine as default_engine
from api.models import Listing, ListingStatus
from connectors.ebay import EbayClient


def check_ebay_listings(client: EbayClient | None = None, db_engine: Engine | None = None) -> tuple[int, int]:
    """client/db_engine are injectable so tests can run against a fake client
    and a throwaway DB instead of hitting eBay or Postgres."""
    client = client or EbayClient()
    db_engine = db_engine or default_engine
    checked = 0
    marked_sold = 0

    with Session(db_engine) as session:
        active = session.exec(
            select(Listing).where(Listing.source == "ebay", Listing.status == ListingStatus.active)
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


if __name__ == "__main__":
    checked, marked_sold = check_ebay_listings()
    print(f"Checked {checked} listings, marked {marked_sold} as likely_sold")

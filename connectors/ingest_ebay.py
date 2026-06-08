"""Stage 1 ingestion entrypoint: runs every configured SavedSearch, normalizes
each result, and upserts. Run manually for now (`python -m connectors.ingest_ebay`);
gets wired into the Redis scheduler in stage 2.
"""

from __future__ import annotations

from sqlalchemy import Engine
from sqlmodel import Session, select

from api.db import engine as default_engine
from api.models import Listing, SavedSearch
from connectors.ebay import EbayClient
from connectors.normalizer import normalize_ebay_item


def ingest_saved_search(
    saved_search: SavedSearch, client: EbayClient | None = None, db_engine: Engine | None = None
) -> int:
    """client/db_engine are injectable so tests can run against a fake client
    and a throwaway DB instead of hitting eBay or Postgres.

    saved_search.location isn't passed to eBay yet: the Browse API doesn't
    support free-text proximity search the way this schema implies (it only
    has country-level delivery/pickup filters). Revisit once a genuinely
    local source (Facebook Marketplace) needs it for real, see LEARNING_LOG.md.
    """
    client = client or EbayClient()
    db_engine = db_engine or default_engine
    raw_items = client.search_items(saved_search.keyword)

    upserted = 0
    with Session(db_engine) as session:
        for raw in raw_items:
            listing = normalize_ebay_item(raw)

            existing = session.exec(
                select(Listing).where(
                    Listing.source == listing.source,
                    Listing.source_id == listing.source_id,
                )
            ).first()

            if existing:
                existing.price = listing.price
                existing.last_seen_at = listing.last_seen_at
                session.add(existing)
            else:
                session.add(listing)

            upserted += 1

        session.commit()

    return upserted


def ingest_all(client: EbayClient | None = None, db_engine: Engine | None = None) -> int:
    """Runs ingest_saved_search for every SavedSearch row in the DB. There's
    no saved-search UI or CRUD yet (that's stage 5); rows come from the seed
    migration or get added by hand for now."""
    db_engine = db_engine or default_engine
    with Session(db_engine) as session:
        saved_searches = session.exec(select(SavedSearch)).all()

    total = 0
    for saved_search in saved_searches:
        total += ingest_saved_search(saved_search, client=client, db_engine=db_engine)
    return total


if __name__ == "__main__":
    count = ingest_all()
    print(f"Upserted {count} listings across all saved searches")

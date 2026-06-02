"""Stage 1 ingestion entrypoint: one hardcoded search -> normalize -> upsert.

Run manually for now (`python -m connectors.ingest_ebay`); gets wired into
the Redis scheduler in stage 2.
"""

from __future__ import annotations

from sqlalchemy import Engine
from sqlmodel import Session, select

from api.db import engine as default_engine
from api.models import Listing
from connectors.ebay import EbayClient
from connectors.normalizer import normalize_ebay_item

SEARCH_QUERY = "nintendo switch"


def ingest(query: str = SEARCH_QUERY, client: EbayClient | None = None, db_engine: Engine | None = None) -> int:
    """client/db_engine are injectable so tests can run against a fake client
    and a throwaway DB instead of hitting eBay or Postgres."""
    client = client or EbayClient()
    db_engine = db_engine or default_engine
    raw_items = client.search_items(query)

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


if __name__ == "__main__":
    count = ingest()
    print(f"Upserted {count} listings from eBay for query={SEARCH_QUERY!r}")

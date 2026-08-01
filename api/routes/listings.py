from collections.abc import Sequence
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session, col, select

from api.db import get_session
from api.models import Listing, ListingRead, ListingStatus, PriceObservation

router = APIRouter(prefix="/listings", tags=["listings"])

# Capped rather than unbounded. The route previously returned the whole table,
# which was survivable at a few hundred rows and is not at ten thousand plus a
# 512-float vector each. 200 matches the eBay Browse page size already used
# throughout the connectors, so paging feels consistent across the project.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@router.get("", response_model=list[ListingRead])
def list_listings(
    session: Session = Depends(get_session),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    source: str | None = Query(None, description="Filter to one marketplace, e.g. 'ebay'"),
    status: ListingStatus | None = Query(None, description="Filter by listing status"),
) -> Sequence[Listing]:
    """Newest first. See ListingRead for what's returned and what isn't."""
    statement = select(Listing)
    if source is not None:
        statement = statement.where(Listing.source == source)
    if status is not None:
        statement = statement.where(Listing.status == status)

    statement = statement.order_by(col(Listing.first_seen_at).desc()).offset(offset).limit(limit)
    return session.exec(statement).all()


class PricePoint(BaseModel):
    observed_at: datetime
    price: float
    # None means shipping was unknown at that observation, not free. Charting
    # it as zero would draw a delivered-cost line that never existed.
    shipping_cost: float | None
    total_cost: float | None


@router.get("/{listing_id}/prices", response_model=list[PricePoint])
def listing_price_history(
    listing_id: int, session: Session = Depends(get_session)
) -> list[PricePoint]:
    """Every recorded price for one listing, oldest first.

    Observations are written only when price or shipping actually moved (plus
    one at insert), so this is a step function with real edges rather than a
    dense series: two points a week apart mean the price held, not that
    nothing was recorded. A chart should draw it stepped, not interpolated.
    """
    observations = session.exec(
        select(PriceObservation)
        .where(PriceObservation.listing_id == listing_id)
        .order_by(col(PriceObservation.observed_at).asc())
    ).all()
    return [
        PricePoint(
            observed_at=o.observed_at,
            price=o.price,
            shipping_cost=o.shipping_cost,
            total_cost=None if o.shipping_cost is None else o.price + o.shipping_cost,
        )
        for o in observations
    ]

from collections.abc import Sequence

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, col, select

from api.db import get_session
from api.models import Listing, ListingRead, ListingStatus

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

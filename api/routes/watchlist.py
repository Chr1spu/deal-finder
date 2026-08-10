"""GET/POST/DELETE /watchlist: listings the user is tracking individually.

The deal feed is a snapshot of the corpus and is thrown away and rebuilt by
every scan. This is the other axis: one listing, followed over time, which is
the question the feed structurally cannot answer because a listing that stops
being a bargain (or stops existing) simply leaves it.

Everything shown here is already being recorded. `PriceObservation` rows are
written whenever a price actually changes, and the disappearance check writes
`status`, `missing_since` and `sale_confidence` on the same schedule for every
active listing. So this route collects nothing new; it joins things that were
already true and puts a person's attention next to them.

The interesting field is `price_change_since_added`. A price drop on a listing
someone already thought was worth watching is the strongest single signal this
system can produce, and it is stronger than the deal score, because the deal
score compares against comps that are themselves asking prices while this
compares a listing against its own earlier self.

Writes carry the API key; the read is open like every other read here.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlmodel import Session, col, select

from api.auth import RequireApiKey
from api.db import engine
from api.models import Listing, ListingStatus, PriceObservation, WatchlistItem

router = APIRouter(prefix="/watchlist", tags=["watchlist"])
WRITE = [RequireApiKey]

# A chart needs points, not the whole history, and a listing checked every two
# hours for months would otherwise return thousands. Newest first, truncated.
MAX_HISTORY_POINTS = 200


class PricePoint(BaseModel):
    price: float
    shipping_cost: float | None
    observed_at: datetime


class WatchlistRead(BaseModel):
    listing_id: int
    title: str
    url: str
    source: str
    category: str | None
    note: str | None
    added_at: datetime

    price_when_added: float
    current_price: float
    currency: str
    shipping_cost: float | None
    total_cost: float | None

    # Negative means cheaper than when it was added, which is the case worth
    # surfacing. Expressed as a fraction rather than a percentage so it reads
    # the same way deal_score does elsewhere in this API.
    price_change_since_added: float

    status: ListingStatus
    missing_since: datetime | None
    # Present only once the listing has left the market. Says how much to
    # believe that it actually sold rather than expiring or being withdrawn,
    # which is the distinction eBay never makes. See ADR 0005.
    sale_confidence: float | None
    price_confidence: float | None

    history: list[PricePoint]


class WatchlistFeed(BaseModel):
    items: list[WatchlistRead]
    # Split out because they answer different questions: how many things am I
    # following, and how many of them are over.
    active_count: int
    ended_count: int
    note: str = (
        "price_change_since_added compares a listing against its own earlier price, "
        "not against comps. It is the one number here that does not depend on the "
        "estimate being right."
    )


class WatchlistCreate(BaseModel):
    listing_id: int
    note: str | None = None


class WatchlistUpdate(BaseModel):
    note: str | None = None


def _read(item: WatchlistItem, listing: Listing, history: list[PriceObservation]) -> WatchlistRead:
    change = 0.0
    if item.price_when_added > 0:
        change = (listing.price - item.price_when_added) / item.price_when_added

    return WatchlistRead(
        listing_id=listing.id or 0,
        title=listing.title,
        url=listing.url,
        source=listing.source,
        category=listing.category,
        note=item.note,
        added_at=item.added_at,
        price_when_added=item.price_when_added,
        current_price=listing.price,
        currency=listing.currency,
        shipping_cost=listing.shipping_cost,
        total_cost=listing.total_cost,
        price_change_since_added=change,
        status=listing.status,
        missing_since=listing.missing_since,
        sale_confidence=listing.sale_confidence,
        price_confidence=listing.price_confidence,
        history=[
            PricePoint(
                price=o.price, shipping_cost=o.shipping_cost, observed_at=o.observed_at
            )
            for o in history
        ],
    )


@router.get("", response_model=WatchlistFeed)
def list_watchlist(
    include_ended: bool = Query(
        default=True,
        description=(
            "Ended listings are kept by default. A listing that sold is the "
            "outcome the watchlist was recording, not clutter."
        ),
    ),
) -> WatchlistFeed:
    with Session(engine) as session:
        items = session.exec(
            select(WatchlistItem).order_by(col(WatchlistItem.added_at).desc())
        ).all()
        if not items:
            return WatchlistFeed(items=[], active_count=0, ended_count=0)

        listing_ids = [i.listing_id for i in items]
        listings = {
            listing.id: listing
            for listing in session.exec(
                select(Listing).where(col(Listing.id).in_(listing_ids))
            ).all()
        }

        # One query for every watched listing's history rather than one per
        # item. A watchlist is small, but N+1 in a loop over user-controlled
        # rows is the kind of thing that is fine until it is not.
        observations: dict[int, list[PriceObservation]] = {}
        for observation in session.exec(
            select(PriceObservation)
            .where(col(PriceObservation.listing_id).in_(listing_ids))
            .order_by(col(PriceObservation.observed_at).asc())
        ).all():
            observations.setdefault(observation.listing_id, []).append(observation)

        out: list[WatchlistRead] = []
        active = ended = 0
        for item in items:
            listing = listings.get(item.listing_id)
            if listing is None:  # pragma: no cover - FK makes this unreachable
                continue
            if listing.status == ListingStatus.active:
                active += 1
            else:
                ended += 1
                if not include_ended:
                    continue
            history = observations.get(item.listing_id, [])[-MAX_HISTORY_POINTS:]
            out.append(_read(item, listing, history))

        return WatchlistFeed(items=out, active_count=active, ended_count=ended)


@router.post("", response_model=WatchlistRead, status_code=status.HTTP_201_CREATED,
             dependencies=WRITE)
def add_to_watchlist(payload: WatchlistCreate) -> WatchlistRead:
    with Session(engine) as session:
        listing = session.get(Listing, payload.listing_id)
        if listing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No listing {payload.listing_id}.",
            )

        existing = session.exec(
            select(WatchlistItem).where(WatchlistItem.listing_id == payload.listing_id)
        ).first()
        if existing is not None:
            # 409 rather than silently succeeding: re-adding would reset
            # price_when_added, which is the one value on this row that is
            # supposed to be frozen, and losing it silently would make the
            # price-change column quietly wrong instead of visibly absent.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Listing {payload.listing_id} is already on the watchlist, added "
                    f"{existing.added_at.isoformat()} at {existing.price_when_added}. "
                    "Re-adding would reset the price it was added at; PATCH the note "
                    "instead, or DELETE and add again if that reset is what you want."
                ),
            )

        item = WatchlistItem(
            listing_id=payload.listing_id,
            note=payload.note,
            price_when_added=listing.price,
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        return _read(item, listing, [])


@router.patch("/{listing_id}", response_model=WatchlistRead, dependencies=WRITE)
def update_note(listing_id: int, payload: WatchlistUpdate) -> WatchlistRead:
    with Session(engine) as session:
        item = session.exec(
            select(WatchlistItem).where(WatchlistItem.listing_id == listing_id)
        ).first()
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Listing {listing_id} is not on the watchlist.",
            )
        item.note = payload.note
        session.add(item)
        session.commit()
        session.refresh(item)

        listing = session.get(Listing, listing_id)
        assert listing is not None  # FK guarantees it
        return _read(item, listing, [])


@router.delete("/{listing_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=WRITE)
def remove_from_watchlist(listing_id: int) -> Response:
    with Session(engine) as session:
        item = session.exec(
            select(WatchlistItem).where(WatchlistItem.listing_id == listing_id)
        ).first()
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Listing {listing_id} is not on the watchlist.",
            )
        session.delete(item)
        session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

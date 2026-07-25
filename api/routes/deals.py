"""GET /deals and GET /deals/{listing_id}.

The point of the whole project, finally reachable without a Python REPL.

Two deliberate choices about what this returns. It always ships the comps the
estimate was built from, because `CLAUDE.md` requires a match to be surfaced
as a best guess with its evidence rather than a verdict, and a deal score with
no visible comps is exactly the verdict shape. And it always ships the caveats
in words: the estimate is biased high (comps are listings that LEFT the
market, which means sold, expired or withdrawn), and a reader who sees only a
percentage will not reconstruct that.

Reads from a cache the scheduled scan writes, because a full scan is minutes
of k-NN queries and cannot run inside a request. See systems/deal_scan.py.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ml.valuation import Valuation, value_listing
from systems.deal_scan import cached_deals, last_scan_at

router = APIRouter(prefix="/deals", tags=["deals"])


class CompRead(BaseModel):
    listing_id: int | None
    title: str
    price: float
    url: str
    sale_confidence: float | None
    price_confidence: float | None


class DealRead(BaseModel):
    listing_id: int | None
    title: str
    url: str
    source: str
    category: str | None
    asking_price: float
    # None when shipping is unknown, deliberately: treating unknown shipping as
    # free makes an item look cheaper than it is, in exactly the comparison
    # this endpoint exists for.
    total_cost: float | None
    estimated_value: float | None
    deal_score: float | None
    confidence: float
    comp_count: int
    comps: list[CompRead]
    signals: dict
    caveats: list[str]


class DealFeed(BaseModel):
    deals: list[DealRead]
    last_scan_at: datetime | None
    note: str


FEED_NOTE = (
    "Deal scores compare an asking price against what comparable listings were asking "
    "when they LEFT the market, which is not the same as what they sold for. Read the "
    "comps, not just the percentage."
)
NEVER_SCANNED_NOTE = (
    "No scan has completed yet. The deal scan runs on a schedule and takes a few minutes; "
    "results appear here once it finishes."
)


def _to_deal_read(listing, valuation: Valuation) -> DealRead:
    return DealRead(
        listing_id=listing.id,
        title=listing.title,
        url=listing.url,
        source=listing.source,
        category=listing.category,
        asking_price=listing.price,
        total_cost=listing.total_cost,
        estimated_value=valuation.estimated_value,
        deal_score=valuation.deal_score,
        confidence=valuation.confidence,
        comp_count=valuation.comp_count,
        comps=[
            CompRead(
                listing_id=c.listing.id,
                title=c.listing.title,
                price=c.price,
                url=c.listing.url,
                sale_confidence=c.listing.sale_confidence,
                price_confidence=c.listing.price_confidence,
            )
            for c in valuation.comps
        ],
        signals=valuation.signals,
        caveats=valuation.caveats,
    )


@router.get("", response_model=DealFeed)
def list_deals(
    limit: int = Query(20, ge=1, le=100),
    min_deal_score: float = Query(0.2, ge=0.0, le=1.0),
    min_confidence: float = Query(0.3, ge=0.0, le=1.0),
) -> DealFeed:
    """Underpriced listings from the most recent completed scan.

    Serves cached results rather than scanning on request: a full scan is
    thousands of k-NN queries and takes minutes, which is a background job's
    shape, not an HTTP request's.
    """
    scanned_at = last_scan_at()
    deals = [
        d
        for d in cached_deals()
        if (d.deal_score or 0.0) >= min_deal_score and d.confidence >= min_confidence
    ]
    return DealFeed(
        deals=deals[:limit],
        last_scan_at=scanned_at,
        note=FEED_NOTE if scanned_at else NEVER_SCANNED_NOTE,
    )


@router.get("/{listing_id}", response_model=DealRead)
def get_deal(listing_id: int) -> DealRead:
    """Value one listing on demand.

    Unlike the feed this is computed live, because it is a single listing and
    costs one k-NN query rather than thousands.
    """
    from sqlmodel import Session

    from api.db import engine
    from api.models import Listing

    valuation = value_listing(listing_id)
    if valuation is None:
        raise HTTPException(status_code=404, detail=f"no listing with id {listing_id}")

    with Session(engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:  # pragma: no cover - value_listing already checked
            raise HTTPException(status_code=404, detail=f"no listing with id {listing_id}")
        session.expunge(listing)

    return _to_deal_read(listing, valuation)

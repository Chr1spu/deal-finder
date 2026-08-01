"""POST /capture and GET /capture/{id}/match.

The push-based intake path for Depop and Facebook Marketplace, per
docs/decisions/0010-depop-is-push-based-now.md. The browser extension parses a
page the user is already viewing and posts it here; no server in this project
ever requests one of those pages itself.

POST requires an X-API-Key header (docs/decisions/0017-api-key-auth.md). The
GET is left open: it reads the user's own corpus and the extension needs it to
show a match. Note that writes fail CLOSED when no key is configured, so a
fresh checkout refuses captures until API_KEY is set, on purpose.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from api.auth import RequireApiKey
from connectors.capture import CapturedListing, save_capture
from ml.match import MatchResult, match_listing
from systems.queue import enqueue_embed_pending

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/capture", tags=["capture"])


class CandidateRead(BaseModel):
    listing_id: int | None
    title: str
    price: float
    total_cost: float | None
    url: str
    similarity: float
    epid: str | None


class PriceContextRead(BaseModel):
    candidate_count: int
    median_price: float
    min_price: float
    max_price: float
    median_total_cost: float | None
    listings_with_known_shipping: int
    spread_ratio: float


class MatchRead(BaseModel):
    """What the extension shows the user.

    `note` carries the honest caveat in words rather than leaving the caller
    to infer it from numbers, because the single most likely misreading of
    this payload is treating asking prices as market value.
    """

    listing_id: int | None
    title: str
    price: float
    total_cost: float | None
    source: str
    analysed: bool
    matched_by: str
    confidence: float
    comps_from: str
    epid: str | None
    candidates: list[CandidateRead]
    price_context: PriceContextRead | None
    note: str


class CaptureRead(BaseModel):
    listing_id: int | None
    created: bool
    analysed: bool
    match: MatchRead | None


ASKING_PRICE_CAVEAT = (
    "Candidate prices are what comparable eBay listings are ASKING, not what they sold for. "
    "Sold-price history is still accumulating, so treat this as context, not valuation."
)
# Shown on every analysed match, not only suspicious ones. Measured end to end:
# a "Switch OLED w/ box" capture matched six "TABLET ONLY" listings at a 1.04
# spread and confidence 1.00. The numbers looked excellent and the match was
# wrong, because visual similarity cannot see what is in the box. Anything that
# reports only the confident-looking figures actively misleads.
BUNDLE_CAVEAT = (
    "Matched on appearance, which cannot tell a bundle from a bare unit "
    "(console-only vs. with dock and controllers, for instance). Check the candidate "
    "titles before trusting the comparison."
)
NOT_ANALYSED_NOTE = (
    "Not analysed yet: this listing has no image embedding. The ML worker embeds new "
    "listings on a schedule, so retry shortly."
)
WIDE_SPREAD_NOTE = (
    "Candidate prices vary widely, which usually means these are not really the same "
    "product. Visual similarity finds items that look alike, which is not the same as "
    "items that are alike. Treat this match as weak."
)


def _to_match_read(result: MatchResult) -> MatchRead:
    analysed = bool(result.candidates)
    context = result.price_context

    note = f"{BUNDLE_CAVEAT} {ASKING_PRICE_CAVEAT}" if analysed else NOT_ANALYSED_NOTE
    # A wide spread is the measured signature of a bad candidate set (0009
    # saw $578 to $3,000 across "matching" prebuilt PCs), so it is surfaced
    # as a warning instead of being averaged into a confident-looking number.
    # Note the absence of this warning proves nothing: see PriceContext.
    if context is not None and context.spread_ratio > 3.0:
        note = f"{WIDE_SPREAD_NOTE} {note}"

    return MatchRead(
        listing_id=result.listing.id,
        title=result.listing.title,
        price=result.listing.price,
        total_cost=result.listing.total_cost,
        source=result.listing.source,
        analysed=analysed,
        matched_by=result.matched_by,
        confidence=result.confidence,
        comps_from=result.comps_from,
        epid=result.epid,
        candidates=[
            CandidateRead(
                listing_id=m.listing.id,
                title=m.listing.title,
                price=m.listing.price,
                total_cost=m.listing.total_cost,
                url=m.listing.url,
                similarity=m.similarity,
                epid=m.listing.epid,
            )
            for m in result.candidates
        ],
        price_context=PriceContextRead(
            candidate_count=context.candidate_count,
            median_price=context.median_price,
            min_price=context.min_price,
            max_price=context.max_price,
            median_total_cost=context.median_total_cost,
            listings_with_known_shipping=context.listings_with_known_shipping,
            spread_ratio=context.spread_ratio,
        )
        if context is not None
        else None,
        note=note,
    )


@router.post(
    "",
    response_model=CaptureRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[RequireApiKey],
)
def capture(payload: CapturedListing, analyse: bool = Query(True)) -> CaptureRead:
    """Store a listing the user captured, and match it against eBay.

    Embedding is enqueued rather than done inline: this process has no torch
    installed, deliberately (see docs/decisions/0009). So a freshly captured
    listing usually comes back `analysed: false` on first call unless its
    photo hash happens to match an eBay listing exactly, and becomes matchable
    once the ML worker picks it up. The extension polls GET below.
    """
    listing, created = save_capture(payload)

    if listing.id is None:  # pragma: no cover - defensive, save_capture commits
        raise HTTPException(status_code=500, detail="capture failed to persist")

    if created or listing.embedded_at is None:
        try:
            enqueue_embed_pending()
        except Exception:
            # Redis being down must not lose the capture. The row is already
            # committed, and embed_pending picks it up on its next scheduled
            # run regardless, since that job selects on embedded_at IS NULL.
            logger.warning("could not enqueue embedding for listing %s", listing.id, exc_info=True)

    result = match_listing(listing.id) if analyse else None
    return CaptureRead(
        listing_id=listing.id,
        created=created,
        analysed=bool(result and result.candidates),
        match=_to_match_read(result) if result else None,
    )


@router.get("/{listing_id}/match", response_model=MatchRead)
def get_match(listing_id: int, k: int = Query(10, ge=1, le=50)) -> MatchRead:
    result = match_listing(listing_id, k=k)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no listing with id {listing_id}")
    return _to_match_read(result)

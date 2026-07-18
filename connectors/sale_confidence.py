"""How much to trust a disappeared listing as a comparable *sale*.

A listing leaves the market when it sells, but also when it expires unsold,
when the seller withdraws it, and when a Best Offer is accepted well below the
asking price we recorded. All but the first bias the comp upward, so treating
every disappearance as a sale at asking price makes every deal look better
than it is. See docs/decisions/0005-sale-confidence.md.

There is no external source of truth available here (eBay's sold-listing pages
are disallowed by their robots.txt, and Marketplace Insights is not grantable),
so confidence is inferred from three things already in the database:

  1. Relist detection: the same photo appearing on another listing means the
     seller relisted rather than sold. This is the strongest signal and costs
     nothing, being a pure database question.
  2. Listing lifetime: vanishing in days looks like a sale, vanishing at
     roughly a standard listing term looks like an expiry.
  3. Best Offer: sold at an unknown discount to the price we recorded.

Pure functions plus one DB-touching lookup, kept separate from
disappearance_check.py so the scoring can be tested without a check loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from api.models import Listing, ListingStatus
from api.settings import settings

# eBay fixed-price listings commonly run in 30-day terms and auto-renew, so a
# listing that vanishes near a multiple of that looks far more like an expiry
# than a sale. Not exact (sellers pick 3/5/7/10/30 day terms, and GTC renews
# silently), which is why this shades confidence rather than deciding outcomes.
TYPICAL_TERM_DAYS = 30
TERM_TOLERANCE_DAYS = 2


# A sale is the base case for "did this sell?"; each signal can push either
# way from here. Not 1.0, because a sale is never actually observed.
BASE_CONFIDENCE = 0.75

# "Is the recorded price what was paid?" starts at certainty, because for a
# plain fixed-price listing with no offers accepted, the asking price simply
# *is* the price. Only specific known ambiguities reduce it.
BASE_PRICE_CONFIDENCE = 1.0


@dataclass
class SaleAssessment:
    """Two scores, deliberately not combined into one.

    sale_confidence answers "did this listing result in a sale?"
    price_confidence answers "is price + shipping what the buyer paid?"

    They're different uncertainties with different consequences, and one
    number can't tell "probably never sold" apart from "definitely sold, at
    a price we can't pin down". Stage 4 should gate comp membership on the
    first and weight the comp's influence by the second.
    See docs/decisions/0007-two-confidences.md.

    Both are ordinal, not probabilities. A 0.675 does not mean 67.5% likely.

    signals is the breakdown, kept so no opaque number hides *why* a comp was
    discounted, and so a bad heuristic shows up in the data rather than being
    argued about from intuition.
    """

    sale_confidence: float
    price_confidence: float
    signals: dict = field(default_factory=dict)


def _relist_window(now: datetime) -> datetime:
    return now - timedelta(days=settings.relist_grace_days)


def find_relist(
    listing: Listing, db_engine: Engine, now: datetime | None = None
) -> Listing | None:
    """Another listing from the same source using the same photo.

    That's strong evidence the seller relisted rather than sold, which on eBay
    happens constantly: fixed-price listings auto-renew under brand new item
    ids. Matches against listings that are currently active, or that first
    appeared within the grace window around the disappearance, so a relist
    posted slightly before the old one lapsed is still caught.

    Returns None when the listing has no image_hash, since absence of evidence
    isn't evidence here and a null hash must not read as "definitely sold".
    """
    if not listing.image_hash:
        return None

    now = now or datetime.now(tz=listing.first_seen_at.tzinfo)

    with Session(db_engine) as session:
        return session.exec(
            select(Listing)
            .where(
                Listing.source == listing.source,
                col(Listing.id) != listing.id,
                Listing.image_hash == listing.image_hash,
                Listing.status != ListingStatus.likely_sold,
                Listing.first_seen_at >= _relist_window(now),
            )
            .order_by(col(Listing.first_seen_at).desc())
        ).first()


def score_sale(
    listing: Listing,
    relist: Listing | None = None,
    now: datetime | None = None,
) -> SaleAssessment:
    """Assess a disappearance: did it sell, and is the recorded price real?

    Two separate scores rather than one, because those are different
    questions with different consequences and one number can't distinguish
    "probably never sold" from "definitely sold at an unclear price".
    See docs/decisions/0007-two-confidences.md.

    Within each score, deliberately multiplicative: the signals are close to
    independent, and a listing that is both a probable relist *and* ran to
    term should score worse than either alone. Additive penalties would let a
    strong signal get diluted by weak ones.

    Every multiplier here is a judgement call with no data behind it yet.
    That's why `signals` records which ones fired: when real outcomes exist,
    each weight can be checked and corrected independently.
    """
    signals: dict = {}

    # --- did it sell? ----------------------------------------------------
    sale = BASE_CONFIDENCE

    if relist is not None:
        # The item is still on the market under a new id, so it didn't sell.
        # Not zero, because image_hash matches on stock photography (common for
        # boxed retail goods) and two sellers can genuinely use the same photo.
        signals["relisted_as"] = relist.source_id
        sale *= 0.15

    # An auction that never received a bid did not sell, whatever its
    # disappearance looks like. This is close to decisive, and it's the one
    # place in this module where the answer isn't really an inference.
    # See docs/decisions/0006-capture-what-ebay-already-sends.md.
    if listing.is_auction and listing.bid_count is not None:
        signals["bid_count"] = listing.bid_count
        if listing.bid_count == 0:
            signals["auction_no_bids"] = True
            sale *= 0.05
        else:
            signals["auction_had_bids"] = True
            sale *= 1.5

    # Only meaningful for fixed-price listings, where ending early means it
    # sold and running to term means nobody bought it. An auction ends at its
    # scheduled end date by definition, sold or not, so reading that as
    # evidence of a non-sale would penalise every auction ever listed,
    # including ones that clearly sold with a dozen bids.
    ended_at_term = None if listing.is_auction else _ended_at_scheduled_end(listing, now)
    if ended_at_term is not None:
        signals["ended_at_scheduled_end"] = ended_at_term
        if ended_at_term:
            sale *= 0.4

    lifetime_days = _lifetime_days(listing, now)
    if lifetime_days is not None:
        signals["lifetime_days"] = round(lifetime_days, 1)
        # Only fall back to guessing at standard terms when eBay didn't tell
        # us the real end date. Rows ingested before 0007 have no
        # item_end_date, so this path stays live for a while yet.
        if ended_at_term is None and _looks_like_term_expiry(lifetime_days):
            signals["near_listing_term"] = True
            signals["term_inferred"] = True
            sale *= 0.5
        elif lifetime_days <= settings.quick_sale_days:
            # Gone fast. Short-lived listings are the ones most likely to have
            # actually sold, and they're also the interesting ones for pricing.
            signals["quick_disappearance"] = True
            sale *= 1.2

    # --- is the recorded price what was actually paid? -------------------
    # Note what is deliberately absent here: nothing about relists, bids or
    # lifetimes. Those bear on whether a sale happened, not on what a buyer
    # handed over, and letting them leak into this score is exactly the
    # conflation 0007 exists to undo.
    price = BASE_PRICE_CONFIDENCE

    if listing.accepts_best_offer:
        # It very likely sold, just not necessarily at the asking price we
        # recorded, and the gap is unknown. Biased high.
        signals["accepts_best_offer"] = True
        signals["price_bias"] = "over"
        price *= 0.75

    if listing.is_auction:
        # The recorded figure is whatever bid was last observed, and bidding
        # only goes up, so the true hammer price is at least this and probably
        # more. Biased low, which is the opposite direction from Best Offer:
        # recording the direction leaves stage 4 the option of correcting a
        # comp rather than merely discounting it.
        signals["is_auction"] = True
        signals["price_bias"] = "under"
        price *= 0.6

    if listing.shipping_cost is None:
        # What a buyer actually paid is price plus shipping, and one of those
        # is unknown. Mild, since item price dominates, but not nothing.
        signals["shipping_unknown"] = True
        price *= 0.9
    elif listing.shipping_estimated:
        # A CALCULATED-only shipping cost depends on the buyer's location, so
        # the figure recorded may have been worked out for somewhere else.
        # Lighter than not knowing at all: it's the right order of magnitude.
        signals["shipping_estimated"] = True
        price *= 0.95

    return SaleAssessment(
        sale_confidence=_clamp(sale),
        price_confidence=_clamp(price),
        signals=signals,
    )


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _ended_at_scheduled_end(listing: Listing, now: datetime | None) -> bool | None:
    """Did the listing disappear at roughly the end date eBay published?

    Returns None when there's no published end date to compare against, which
    means Good 'Til Cancelled (eBay omits itemEndDate for those) or a row
    ingested before 0007 captured the field. Callers must treat None as
    "unknown" and fall back, not as "no".

    This replaces guessing at 30-day terms with the actual scheduled end,
    which is the difference between a heuristic and a fact.
    """
    if listing.item_end_date is None:
        return None
    ended = listing.missing_since or now
    if ended is None:
        return None
    gap_hours = abs((ended - listing.item_end_date).total_seconds()) / 3600
    # Generous, because the check runs on an interval and only notices a
    # disappearance some hours after it happens.
    return gap_hours <= settings.scheduled_end_tolerance_hours


def _lifetime_days(listing: Listing, now: datetime | None) -> float | None:
    """How long the listing was on the market before it vanished.

    Uses missing_since (the *first* failed lookup) rather than the confirming
    one, since that's closer to when the item actually left. Returns None when
    posted_at is unknown, so the signal is skipped rather than guessed.
    """
    if listing.posted_at is None:
        return None
    ended = listing.missing_since or now
    if ended is None:
        return None
    return (ended - listing.posted_at).total_seconds() / 86400


def _looks_like_term_expiry(lifetime_days: float) -> bool:
    """True near a whole multiple of a standard listing term, since a GTC
    listing that never sells lapses or renews on that cycle."""
    if lifetime_days < TYPICAL_TERM_DAYS - TERM_TOLERANCE_DAYS:
        return False
    offset = lifetime_days % TYPICAL_TERM_DAYS
    return offset <= TERM_TOLERANCE_DAYS or offset >= TYPICAL_TERM_DAYS - TERM_TOLERANCE_DAYS

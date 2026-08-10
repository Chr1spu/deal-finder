"""What is this worth, and is this listing a good deal?

Stage 4. Everything before this produced inputs; this is the first module that
answers the question the project exists for.

Four rules, each traceable to an earlier decision:

  sold comps only      active listings are asking prices. ml/match.py's
                       PriceContext already reports those and says so.
  gate on sale,        0007 split "did it sell" from "is the price real"
  weight by price      precisely so stage 4 could use them differently. A comp
                       that probably never sold is excluded outright; one that
                       sold at an unclear price is included and counts less.
  delivered cost       price + shipping where known (0008), because a Facebook
                       pickup has no shipping and an eBay comp does.
  refuse to guess      below MIN_COMPS_FOR_VALUATION, the answer is None. A
                       confident-looking number gets acted on; a missing one
                       does not.

**The honest headline risk.** Every input is biased the same way and the bias
compounds. A listing leaves the market because it sold, *or* expired unsold,
*or* was withdrawn (0003), and Best Offer sales settle below asking (0005).
So the value estimate is biased HIGH and the deal score is therefore biased
OPTIMISTIC: it will say things are better deals than they are. That is the
wrong direction for a tool whose output is "you should buy this", so it is
surfaced on every valuation rather than buried here.

See docs/decisions/0014-valuation-and-deal-scoring.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from api.db import engine as default_engine
from api.models import Listing, ListingStatus
from api.settings import settings
from ml.similar import COMP_SOURCE, find_similar_to_vector

# How wide a neighbourhood to search before filtering down to sold rows. Sold
# listings are a small fraction of the corpus (1,001 of ~13,000), so a k of 10
# would usually surface none at all. Measured at k=60: 69% of listings find
# three or more sold comps and the median is five.
COMP_SEARCH_K = 60

# Caveats are returned as text rather than left for the caller to infer from
# numbers, because the failure modes here are conceptual rather than numeric
# and a reader who sees only a percentage will not reconstruct them.
UPWARD_BIAS_CAVEAT = (
    "Comps are listings that LEFT THE MARKET, which means sold, expired unsold, or "
    "withdrawn. eBay does not distinguish them. Values are therefore biased high and "
    "deal scores biased optimistic."
)
THIN_COMPS_CAVEAT = "Few comps, so this estimate is unstable and a single odd listing moves it."
WIDE_SPREAD_CAVEAT = (
    "Comp prices vary widely, which usually means they are not all the same product. "
    "Treat the estimate as weak."
)
BEST_OFFER_CAVEAT = (
    "Several comps accepted Best Offer, so their recorded prices are above what buyers "
    "actually paid. The real value is likely lower than this estimate."
)


@dataclass
class Comp:
    """One sold listing standing as evidence, with the two scores that decide
    how much it counts. Returned alongside the estimate so a user can see what
    the number was built from rather than being asked to trust it."""

    listing: Listing
    price: float
    weight: float


@dataclass
class Valuation:
    estimated_value: float | None
    comp_count: int
    confidence: float
    deal_score: float | None
    comps: list[Comp] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)

    @property
    def has_estimate(self) -> bool:
        return self.estimated_value is not None


def _comp_price(listing: Listing) -> tuple[float, bool]:
    """Delivered cost where shipping is known, item price otherwise.

    Returns (price, was_substituted) so the substitution is recorded rather
    than silent: falling back to item price understates what a buyer paid,
    which pushes the estimate down and the deal score with it.
    """
    total = listing.total_cost
    if total is not None:
        return total, False
    return listing.price, True


def weighted_median(values: list[tuple[float, float]]) -> float | None:
    """Median of (value, weight) pairs.

    Median rather than mean because residual comp spread is still 2.74x after
    0013's filtering, so outliers are present and a mean would chase them.
    Weighted because 0007 says price confidence should scale a comp's
    influence: a Best Offer sale counts, just less.
    """
    if not values:
        return None
    ordered = sorted(values, key=lambda pair: pair[0])
    total = sum(weight for _, weight in ordered)
    if total <= 0:
        # Every weight zero: fall back to the unweighted median rather than
        # returning nothing, since the prices themselves are still evidence.
        midpoint = len(ordered) // 2
        return ordered[midpoint][0]

    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= total / 2:
            return value
    return ordered[-1][0]  # pragma: no cover - unreachable while weights are finite


def find_sold_comps(
    listing: Listing, db_engine: Engine | None = None, k: int = COMP_SEARCH_K
) -> list[Comp]:
    """Sold eBay listings comparable to this one.

    Retrieval reuses the same spec filtering as ml/match.py, then keeps only
    rows that actually left the market and whose sale is credible enough to
    count. Note what is NOT used as a weight: CLIP similarity. 0009 measured
    it running anti-correlated with comp quality, so weighting by it would
    trust the worst comps most. It gates retrieval and nothing else.
    """
    db_engine = db_engine or default_engine
    if listing.embedding is None:
        return []

    neighbours = find_similar_to_vector(
        list(listing.embedding),
        k=k,
        db_engine=db_engine,
        source=COMP_SOURCE,
        exclude_listing_id=listing.id,
        comparable_only=True,
        completeness=listing.completeness,
        capacity_gb=listing.capacity_gb,
        spec_generation=listing.spec_generation,
        form_factor=listing.form_factor,
        model_key=listing.model_key,
        category=listing.category,
    )

    comps: list[Comp] = []
    for match in neighbours:
        candidate = match.listing
        if candidate.status != ListingStatus.likely_sold:
            continue
        # Gate on sale confidence. A relisted item that probably never sold is
        # not weak evidence of a sale price, it is evidence of nothing.
        sale = candidate.sale_confidence
        if sale is not None and sale < settings.min_sale_confidence:
            continue
        price, _ = _comp_price(candidate)
        # price_confidence defaults to 1.0 when unscored: for a plain
        # fixed-price listing the asking price simply is the price (0007).
        comps.append(Comp(listing=candidate, price=price, weight=candidate.price_confidence or 1.0))
    return comps


def _confidence(comps: list[Comp], spread: float) -> tuple[float, dict]:
    """Ordinal, never a probability. Same convention as sale_confidence, and
    for the same reason: these weights are judgement, not measurement."""
    signals: dict = {"comp_count": len(comps)}
    score = 1.0

    if len(comps) < 5:
        # Three or four comps is enough to answer at all and not enough to be
        # steady. Scales rather than steps, so five is not a magic cliff.
        score *= 0.5 + 0.1 * len(comps)
        signals["few_comps"] = True

    if spread > 2.0:
        score *= 0.6
        signals["wide_spread"] = round(spread, 2)

    sales = [c.listing.sale_confidence for c in comps if c.listing.sale_confidence is not None]
    if sales:
        median_sale = sorted(sales)[len(sales) // 2]
        signals["median_sale_confidence"] = round(median_sale, 3)
        score *= median_sale

    prices = [c.weight for c in comps]
    median_price_conf = sorted(prices)[len(prices) // 2] if prices else 1.0
    signals["median_price_confidence"] = round(median_price_conf, 3)
    score *= median_price_conf

    return max(0.0, min(1.0, score)), signals


def value_listing(
    listing_id: int, db_engine: Engine | None = None, k: int = COMP_SEARCH_K
) -> Valuation | None:
    """Estimate what a listing is worth and how good a deal it is.

    Returns None if the listing does not exist. Returns a Valuation with
    `estimated_value=None` when there are too few comps, which is an answer
    ("not enough evidence") rather than a failure, and is currently the case
    for roughly a third of listings.
    """
    db_engine = db_engine or default_engine
    with Session(db_engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return None
        session.expunge(listing)

    comps = find_sold_comps(listing, db_engine=db_engine, k=k)
    caveats = [UPWARD_BIAS_CAVEAT]

    if len(comps) < settings.min_comps_for_valuation:
        return Valuation(
            estimated_value=None,
            comp_count=len(comps),
            confidence=0.0,
            deal_score=None,
            comps=comps,
            signals={"comp_count": len(comps), "below_minimum": True},
            caveats=[THIN_COMPS_CAVEAT, *caveats],
        )

    prices = [c.price for c in comps]
    spread = max(prices) / min(prices) if min(prices) > 0 else float("inf")
    estimate = weighted_median([(c.price, c.weight) for c in comps])
    confidence, signals = _confidence(comps, spread)
    signals["spread_ratio"] = round(spread, 2)

    if spread > 2.0:
        caveats.append(WIDE_SPREAD_CAVEAT)
    if sum(1 for c in comps if c.listing.accepts_best_offer) >= max(1, len(comps) // 3):
        caveats.append(BEST_OFFER_CAVEAT)
    if len(comps) < 5:
        caveats.append(THIN_COMPS_CAVEAT)

    asking, substituted = _comp_price(listing)
    if substituted:
        signals["asking_price_excludes_unknown_shipping"] = True

    # Positive means the asking price is below the estimate, i.e. a deal.
    # Expressed as a fraction of the estimate so it reads as a percentage.
    deal_score = None
    if estimate and estimate > 0:
        deal_score = (estimate - asking) / estimate

    return Valuation(
        estimated_value=estimate,
        comp_count=len(comps),
        confidence=confidence,
        deal_score=deal_score,
        comps=comps,
        signals=signals,
        caveats=caveats,
    )


def deal_candidates(db_engine: Engine | None = None, scan_limit: int | None = None) -> list[int]:
    """Active listings worth valuing at all.

    Restricted to listings that share a `model_key` or `epid` with something
    already sold, which is the cheap way to skip the ~40% that would find no
    comps anyway. Without it a full scan values all 12,400 active listings and
    spends a k-NN query on each, which takes ten minutes and throws most of
    the work away.
    """
    db_engine = db_engine or default_engine
    with Session(db_engine) as session:
        sold_models = {
            m for m in session.exec(
                select(Listing.model_key).where(
                    Listing.status == ListingStatus.likely_sold,
                    col(Listing.model_key).is_not(None),
                )
            ).all() if m
        }
        sold_epids = {
            e for e in session.exec(
                select(Listing.epid).where(
                    Listing.status == ListingStatus.likely_sold,
                    col(Listing.epid).is_not(None),
                )
            ).all() if e
        }

        statement = select(Listing.id).where(
            Listing.source == COMP_SOURCE,
            Listing.status == ListingStatus.active,
            col(Listing.embedding).is_not(None),
            col(Listing.lot_size).is_(None),
            col(Listing.has_defect).is_(False),
            col(Listing.is_accessory).is_(False),
            col(Listing.price_is_from).is_(False),
            # An auction's `price` is the current bid, not an asking price, and
            # with no bids it is an opening bid the seller set deliberately low
            # to attract them. Scoring that as a discount measures the seller's
            # marketing, not the market: a "PNY RTX 4090 Verto 24gb" opened at
            # $107.87 and read as a 95% discount against a $2,699.99 estimate.
            #
            # ADR 0004 captured `is_auction` precisely because unflagged
            # auctions "would have quietly poisoned stage 4's comps", then left
            # the question of what to do with them for real data. This is the
            # answer for the query side: 451 active auctions, 279 of them with
            # zero bids, none of which has an asking price to discount.
            #
            # Deliberately NOT excluded from the comp side in ml/similar.py. A
            # *sold* auction's final price is a real transaction, which is
            # better evidence than an unsold listing's asking price, and
            # throwing it away would discard the best comps this system has.
            col(Listing.is_auction).is_(False),
        )
        if sold_models or sold_epids:
            statement = statement.where(
                col(Listing.model_key).in_(sold_models) | col(Listing.epid).in_(sold_epids)
            )
        if scan_limit is not None:
            statement = statement.limit(scan_limit)
        return [i for i in session.exec(statement).all() if i is not None]


def find_deals(
    limit: int = 20,
    min_deal_score: float = 0.2,
    min_confidence: float = 0.3,
    db_engine: Engine | None = None,
    scan_limit: int | None = None,
) -> list[tuple[Listing, Valuation]]:
    """Active eBay listings priced below what comparable items sold for.

    The scan half of the Deal Scanner. Deliberately requires BOTH a deal score
    and a confidence: a large apparent discount computed from two shaky comps
    is the most likely thing to be wrong, and is exactly what an unfiltered
    "biggest discounts" list would surface first.

    This is a batch job, not an interactive call. Each candidate costs a k-NN
    query (~30 ms) plus filtering, so cost scales with the candidate count;
    `deal_candidates` narrows that to listings that could plausibly have comps,
    and `scan_limit` bounds it further for a quick look.
    """
    db_engine = db_engine or default_engine
    candidates = deal_candidates(db_engine=db_engine, scan_limit=scan_limit)

    found: list[tuple[Listing, Valuation]] = []
    for listing_id in candidates:
        if listing_id is None:
            continue
        valuation = value_listing(listing_id, db_engine=db_engine)
        if valuation is None or valuation.deal_score is None:
            continue
        if valuation.deal_score >= min_deal_score and valuation.confidence >= min_confidence:
            with Session(db_engine) as session:
                listing = session.get(Listing, listing_id)
                if listing is None:  # pragma: no cover - deleted mid-scan
                    continue
                session.expunge(listing)
            found.append((listing, valuation))

    found.sort(key=lambda pair: pair[1].deal_score or 0.0, reverse=True)
    return found[:limit]

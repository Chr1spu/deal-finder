"""Identify which eBay product a captured listing is, and show the evidence.

This is the bridge the whole project is built around: a Depop or Facebook
listing arrives with a photo and a scrappy title and no eBay catalog id, and
the question is which eBay product it corresponds to. See
docs/decisions/0008-price-oracle-and-valuation-clients.md.

Deliberately stops short of a deal score. It answers "what is this, how sure
am I, and what are those eBay listings asking", and hands back the candidates
with their evidence. Turning that into "this is 30% under market" is stage 4's
job and needs sold-price history that does not exist yet. `CLAUDE.md` is
explicit that a cross-source match is surfaced as a best guess with a
confidence, shown alongside the comps used, rather than silently driving a
score, and keeping the two layers apart is how that stays true.

Order of attack, cheapest and most precise first:

  1. image_hash. A foreign seller reusing a manufacturer or eBay stock photo
     is the same product, provably, for the cost of an indexed lookup and no
     model at all. High precision, low recall.
  2. CLIP embedding k-NN against the eBay reference index (0009). Broad
     recall, and genuinely weak at product identity: it retrieves "three-fan
     graphics card", not "this exact SKU".

Measured caution carried over from 0009, because it inverts the obvious
reading: image-to-image similarity ran *anti*-correlated with comp quality
(interchangeable-looking prebuilt PCs scored 0.87 while more useful iPhone
neighbours scored 0.77). Similarity is therefore reported, never used as a
weight, and never compared against the text-to-image scores, which live on a
different scale entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from api.db import engine as default_engine
from api.models import Listing
from ml.similar import COMP_SOURCE, Match, find_similar_to_vector

# How a candidate was found. Kept as a plain string on the result rather than
# folded into a single number, so the UI can say "same photo" versus "looks
# similar", which mean very different things to someone deciding whether to
# trust a match.
MATCH_BY_IMAGE_HASH = "image_hash"
MATCH_BY_EMBEDDING = "embedding"

# How the *price context* was built, which is a separate question from how the
# listing was identified. Measured 2026-07-25: comp sets keyed on epid have a
# median price spread of 1.50x against 3.61x for raw CLIP neighbours, so once
# identification lands on a listing with a catalog id, the id is a much better
# basis for pricing than the neighbour set that found it.
COMPS_FROM_EPID = "epid"
COMPS_FROM_CANDIDATES = "candidates"

# An identical perceptual hash is treated as certain. phash collisions between
# genuinely different products are rare enough that the failure mode worth
# worrying about is the opposite one: the same stock photo used by a reseller
# for a product that differs in a detail the photo does not show (storage
# size, bundled accessories). Flagged in the docstring rather than discounted,
# since discounting it would just be an invented number.
IMAGE_HASH_CONFIDENCE = 1.0


@dataclass
class PriceContext:
    """What the matched eBay listings are asking.

    ASKING prices, not sold prices, and the distinction is the whole reason
    stage 4 exists separately. Nothing here has been confirmed to have sold,
    so this is "what similar things are listed at", which is an upper-biased
    proxy for value. Named PriceContext rather than anything with "value" or
    "market" in it for exactly that reason.
    """

    candidate_count: int
    median_price: float
    min_price: float
    max_price: float
    # Median of price + shipping across candidates where shipping is known.
    # None when it is unknown for all of them, deliberately rather than
    # falling back to item price, which would make eBay look cheaper than it
    # is and every foreign listing look worse by comparison.
    median_total_cost: float | None
    listings_with_known_shipping: int

    @property
    def spread_ratio(self) -> float:
        """max/min across candidates.

        A wide spread means the candidate set is not really one product, which
        is the failure mode 0009 measured on prebuilt PCs ($578 to $3,000). A
        caller should read a high value as a reason to distrust the match, not
        as a wide market.

        **The converse does NOT hold, and assuming it does is the trap.**
        Measured end to end on 2026-07-25: a captured "Nintendo Switch OLED
        barely used w/ box" matched six eBay listings spanning $125 to $130,
        a spread of 1.04, every one of them "TABLET ONLY" with no dock or
        Joy-Cons. A tight spread means the candidates agree with each other,
        which they will whenever the model has retrieved a coherent set of the
        *wrong* thing. Low spread is necessary for a good match and nowhere
        near sufficient.
        """
        return self.max_price / self.min_price if self.min_price > 0 else float("inf")


@dataclass
class MatchResult:
    listing: Listing
    candidates: list[Match] = field(default_factory=list)
    matched_by: str = MATCH_BY_EMBEDDING
    confidence: float = 0.0
    price_context: PriceContext | None = None
    comps_from: str = COMPS_FROM_CANDIDATES
    epid: str | None = None

    @property
    def is_exact(self) -> bool:
        return self.matched_by == MATCH_BY_IMAGE_HASH


def find_by_image_hash(
    listing: Listing, db_engine: Engine | None = None, source: str = COMP_SOURCE
) -> list[Listing]:
    """eBay listings whose primary photo is byte-for-byte perceptually
    identical to this one. Empty list when the listing has no hash yet."""
    if not listing.image_hash:
        return []

    db_engine = db_engine or default_engine
    with Session(db_engine) as session:
        rows = session.exec(
            select(Listing).where(
                Listing.source == source,
                Listing.image_hash == listing.image_hash,
                col(Listing.id) != listing.id,
            )
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def comps_for_epid(
    epid: str,
    db_engine: Engine | None = None,
    exclude_listing_id: int | None = None,
    completeness: str | None = None,
) -> list[Listing]:
    """Every usable eBay listing for one catalog product.

    Two listings sharing an epid are definitively the same product, so this is
    a far better comp set than the neighbour list that found it: measured, a
    median price spread of 1.50x against 3.61x for raw CLIP neighbours. Lots
    and for-parts units are excluded here for the same reason they are excluded
    from k-NN, since sharing a catalog id does not make a broken unit or a box
    of fifty comparable to a working single item.
    """
    db_engine = db_engine or default_engine
    with Session(db_engine) as session:
        statement = select(Listing).where(
            Listing.source == COMP_SOURCE,
            Listing.epid == epid,
            col(Listing.lot_size).is_(None),
            col(Listing.has_defect).is_(False),
            col(Listing.is_accessory).is_(False),
            col(Listing.price_is_from).is_(False),
        )
        if exclude_listing_id is not None:
            statement = statement.where(col(Listing.id) != exclude_listing_id)
        if completeness is not None:
            # An epid is the same *product*, not the same *package*. Inside one
            # Switch OLED epid sit a bare "Tablet Only" and a full dock-and-
            # cables bundle. Pricing a console-only listing against that mix is
            # the same error as the k-NN path makes without this filter, so the
            # same rule applies: match the stated completeness, keep unstated
            # rows (89% of titles say nothing), drop the stated mismatches.
            statement = statement.where(
                (col(Listing.completeness) == completeness)
                | col(Listing.completeness).is_(None)
            )
        rows = session.exec(statement).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def summarize_prices(candidates: list[Match]) -> PriceContext | None:
    if not candidates:
        return None

    prices = [m.listing.price for m in candidates]
    totals = [m.listing.total_cost for m in candidates if m.listing.total_cost is not None]

    return PriceContext(
        candidate_count=len(candidates),
        median_price=median(prices),
        min_price=min(prices),
        max_price=max(prices),
        median_total_cost=median(totals) if totals else None,
        listings_with_known_shipping=len(totals),
    )


def match_listing(
    listing_id: int, k: int = 10, db_engine: Engine | None = None
) -> MatchResult | None:
    """Identify a captured listing against the eBay index.

    Returns None if the listing does not exist. Returns a MatchResult with an
    empty candidate list if it exists but has not been embedded yet, which is
    an ordinary state (the ML worker runs on its own schedule) rather than an
    error, and the caller should say "not analysed yet" rather than "no match".
    """
    db_engine = db_engine or default_engine
    with Session(db_engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return None
        session.expunge(listing)

    exact = find_by_image_hash(listing, db_engine=db_engine)
    if exact:
        candidates = [Match(listing=row, similarity=IMAGE_HASH_CONFIDENCE) for row in exact]
        return _with_price_context(
            MatchResult(
                listing=listing,
                candidates=candidates,
                matched_by=MATCH_BY_IMAGE_HASH,
                confidence=IMAGE_HASH_CONFIDENCE,
            ),
            db_engine=db_engine,
        )

    if listing.embedding is None:
        return MatchResult(listing=listing, candidates=[], confidence=0.0)

    candidates = find_similar_to_vector(
        list(listing.embedding),
        k=k,
        db_engine=db_engine,
        exclude_listing_id=listing.id,
        # Lots and for-parts units never make a comp set. Completeness filters
        # only when this listing states its own, since 89% of titles are
        # silent and requiring a match would discard nearly everything.
        comparable_only=True,
        completeness=listing.completeness,
        # Spec filters, each applied only when this listing states a value.
        # Capacity alone separates RAM medians from $59.50 (8GB) to $1,975
        # (128GB); generation and form factor take 32GB from an 82.7x spread
        # down to 2.7-4.4x. See docs/decisions/0013-spec-extraction.md.
        capacity_gb=listing.capacity_gb,
        spec_generation=listing.spec_generation,
        form_factor=listing.form_factor,
        model_key=listing.model_key,
        category=listing.category,
    )
    return _with_price_context(
        MatchResult(
            listing=listing,
            candidates=candidates,
            matched_by=MATCH_BY_EMBEDDING,
            # The top neighbour's similarity, reported as-is. Explicitly NOT
            # combined with the price spread or anything else into a single
            # trust score: 0009 measured that this number can run opposite to
            # match quality, so blending it into a confidence would launder a
            # misleading signal into something that looks authoritative.
            confidence=candidates[0].similarity if candidates else 0.0,
        ),
        db_engine=db_engine,
    )


def _with_price_context(result: MatchResult, db_engine: Engine | None = None) -> MatchResult:
    """Fill in the price context, preferring the catalog id over the neighbours.

    Two hops, and separating them is the point. Identification answers "which
    product is this", and image hashing or CLIP does that. Pricing answers
    "what does that product go for", and once identification lands on a listing
    carrying an `epid`, every listing sharing that id is definitively the same
    product, which is a much better basis than the neighbour set that found it.
    Measured 2026-07-25: epid-keyed comp sets have a median price spread of
    1.50x against 3.61x for raw CLIP neighbours, 2.4x tighter.

    Falls back to the candidates themselves when the top match has no epid,
    which is most of the corpus (46% coverage) and nearly all of some
    categories: prebuilt PCs are 3.9%.
    """
    if not result.candidates:
        return result

    top = result.candidates[0].listing
    if top.epid:
        peers = comps_for_epid(
            top.epid,
            db_engine=db_engine,
            exclude_listing_id=result.listing.id,
            completeness=result.listing.completeness,
        )
        if len(peers) >= 2:
            result.epid = top.epid
            result.comps_from = COMPS_FROM_EPID
            # The displayed comps become the epid peers, replacing the
            # neighbours that found them. Otherwise the payload shows one set
            # of listings beside a median computed from a different set, and a
            # reader cannot reconcile "n=12" with the three rows in front of
            # them. The neighbour search has done its job (identification);
            # matched_by and confidence still record how that happened.
            #
            # Similarity is carried from the identifying match rather than
            # recomputed per peer: these are the same catalog product by
            # definition, so a per-peer visual score would imply a distinction
            # that does not exist.
            result.candidates = [
                Match(listing=peer, similarity=result.confidence) for peer in peers
            ]
            result.price_context = summarize_prices(result.candidates)
            return result

    result.comps_from = COMPS_FROM_CANDIDATES
    result.price_context = summarize_prices(result.candidates)
    return result

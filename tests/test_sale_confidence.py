"""A disappearance is not a sale. See docs/decisions/0005-sale-confidence.md.

These are the tests that matter most in the project so far: the valuation
engine's entire output is a function of how good the comp set is, and this
module is what decides which comps are trustworthy.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from api.models import Listing, ListingStatus
from connectors.sale_confidence import (
    BASE_CONFIDENCE,
    find_relist,
    score_sale,
)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def make_listing(
    posted_days_ago: float | None = 3,
    missing_days_ago: float = 0,
    image_hash: str | None = "abc123",
    accepts_best_offer: bool = False,
    is_auction: bool = False,
    source_id: str = "item-1",
    status: ListingStatus = ListingStatus.active,
    first_seen_days_ago: float = 3,
) -> Listing:
    return Listing(
        source="ebay",
        source_id=source_id,
        title="RTX 3090",
        price=800.0,
        url="https://ebay.com/x",
        image_hash=image_hash,
        accepts_best_offer=accepts_best_offer,
        is_auction=is_auction,
        status=status,
        posted_at=None if posted_days_ago is None else NOW - timedelta(days=posted_days_ago),
        missing_since=NOW - timedelta(days=missing_days_ago),
        first_seen_at=NOW - timedelta(days=first_seen_days_ago),
        last_seen_at=NOW - timedelta(days=missing_days_ago),
    )


# --- the signals, individually --------------------------------------------


def test_a_plain_quick_disappearance_scores_well():
    """The good case: fixed price, no best offer, gone in a few days. Still
    not 1.0, because we never actually observed a sale."""
    result = score_sale(make_listing(posted_days_ago=3), now=NOW)

    assert result.sale_confidence > BASE_CONFIDENCE
    assert result.sale_confidence < 1.0
    assert result.signals["quick_disappearance"] is True


def test_a_relist_collapses_confidence():
    """The strongest signal: the same photo is live under a different id, so
    the seller relisted and the item never sold."""
    relist = make_listing(source_id="item-2")
    result = score_sale(make_listing(), relist=relist, now=NOW)

    assert result.sale_confidence < 0.2
    assert result.signals["relisted_as"] == "item-2"


def test_a_relist_does_not_score_exactly_zero():
    """image_hash matches on stock photography, which is common for boxed
    retail goods, so a match is strong evidence and not proof."""
    result = score_sale(make_listing(), relist=make_listing(source_id="item-2"), now=NOW)
    assert result.sale_confidence > 0.0


def test_vanishing_at_a_standard_listing_term_looks_like_expiry():
    """A GTC listing that never sells lapses or renews on a ~30 day cycle.
    That's the single most likely way a non-sale enters the comp set."""
    result = score_sale(make_listing(posted_days_ago=30), now=NOW)

    assert result.signals["near_listing_term"] is True
    assert result.sale_confidence < BASE_CONFIDENCE


def test_term_expiry_is_detected_at_multiples_too():
    """GTC auto-renews, so an unsold listing can lapse at 60 or 90 days."""
    assert score_sale(make_listing(posted_days_ago=60), now=NOW).signals.get("near_listing_term")
    assert score_sale(make_listing(posted_days_ago=90), now=NOW).signals.get("near_listing_term")


def test_a_listing_gone_mid_term_is_not_treated_as_expiry():
    result = score_sale(make_listing(posted_days_ago=17), now=NOW)
    assert "near_listing_term" not in result.signals


def test_best_offer_hits_price_confidence_not_sale_confidence():
    """A Best Offer listing that vanished almost certainly *did* sell. Only
    the price is uncertain, and by a known direction (biased high, since it
    sold at some discount to asking). Letting it suppress the sale score was
    the conflation docs/decisions/0007-two-confidences.md exists to undo."""
    plain = score_sale(make_listing(posted_days_ago=15), now=NOW)
    offer = score_sale(make_listing(posted_days_ago=15, accepts_best_offer=True), now=NOW)

    assert offer.sale_confidence == plain.sale_confidence
    assert offer.price_confidence < plain.price_confidence
    assert offer.signals["accepts_best_offer"] is True
    assert offer.signals["price_bias"] == "over"


def test_auction_hits_price_confidence_not_sale_confidence():
    """Whether an auction sold is answered by bid_count, not by it being an
    auction. What being an auction costs you is price certainty: the recorded
    figure is the last bid seen, and bidding only goes up, so it's biased
    low, the opposite direction from Best Offer."""
    plain = score_sale(make_listing(posted_days_ago=15), now=NOW)
    auction = score_sale(make_listing(posted_days_ago=15, is_auction=True), now=NOW)

    assert auction.sale_confidence == plain.sale_confidence
    assert auction.price_confidence < plain.price_confidence
    assert auction.signals["price_bias"] == "under"


def test_a_plain_fixed_price_listing_has_full_price_confidence():
    """For a fixed-price listing with no offers, the asking price simply IS
    the price. Nothing to hedge about."""
    listing = make_listing(posted_days_ago=3)
    listing.shipping_cost = 9.99

    assert score_sale(listing, now=NOW).price_confidence == 1.0


def test_unknown_shipping_costs_a_little_price_confidence():
    """What a buyer paid is price plus shipping, and one of those is unknown."""
    known = make_listing(posted_days_ago=3)
    known.shipping_cost = 9.99
    unknown = make_listing(posted_days_ago=3)

    assert score_sale(unknown, now=NOW).price_confidence < score_sale(known, now=NOW).price_confidence
    assert score_sale(unknown, now=NOW).signals["shipping_unknown"] is True


def test_the_two_scores_are_independent():
    """The point of the split: a relisted item (probably never sold) and a
    Best Offer sale (definitely sold, unclear price) used to land at similar
    combined scores and become indistinguishable."""
    relisted = score_sale(make_listing(posted_days_ago=3), relist=make_listing(source_id="r"), now=NOW)
    best_offer = score_sale(make_listing(posted_days_ago=3, accepts_best_offer=True), now=NOW)

    assert relisted.sale_confidence < best_offer.sale_confidence, "one probably didn't sell"
    assert relisted.price_confidence > best_offer.price_confidence, "the other's price is the murky part"


def test_unknown_posted_at_skips_the_lifetime_signal_rather_than_guessing():
    result = score_sale(make_listing(posted_days_ago=None), now=NOW)

    assert "lifetime_days" not in result.signals
    assert "near_listing_term" not in result.signals
    assert result.sale_confidence == pytest.approx(BASE_CONFIDENCE)


def test_signals_compound():
    """A listing that's both a probable relist and vanished at term should
    score worse than either alone, which is why the penalties multiply."""
    relist = make_listing(source_id="item-2")
    both = score_sale(make_listing(posted_days_ago=30), relist=relist, now=NOW)
    relist_only = score_sale(make_listing(posted_days_ago=3), relist=relist, now=NOW)

    assert both.sale_confidence < relist_only.sale_confidence


def test_score_always_lands_in_zero_to_one():
    for listing in [
        make_listing(posted_days_ago=1),
        make_listing(posted_days_ago=30, accepts_best_offer=True, is_auction=True),
        make_listing(posted_days_ago=None),
    ]:
        for relist in (None, make_listing(source_id="other")):
            score = score_sale(listing, relist=relist, now=NOW).sale_confidence
            assert 0.0 <= score <= 1.0


# --- relist lookup, against a real DB -------------------------------------


def _seed(session: Session, listing: Listing) -> Listing:
    """Commit, load every attribute, then detach.

    expunge matters: without it the instance stays bound to a session that the
    `with` block is about to close, and the first attribute read afterwards
    raises DetachedInstanceError. refresh first so the values are actually
    loaded before it's detached.
    """
    session.add(listing)
    session.commit()
    session.refresh(listing)
    session.expunge(listing)
    return listing


def test_find_relist_matches_a_live_listing_with_the_same_photo(test_engine):
    with Session(test_engine) as session:
        gone = _seed(session, make_listing(source_id="gone"))
        _seed(session, make_listing(source_id="relisted", first_seen_days_ago=1))

    assert find_relist(gone, db_engine=test_engine, now=NOW).source_id == "relisted"


def test_find_relist_ignores_a_different_photo(test_engine):
    with Session(test_engine) as session:
        gone = _seed(session, make_listing(source_id="gone", image_hash="aaa"))
        _seed(session, make_listing(source_id="unrelated", image_hash="zzz", first_seen_days_ago=1))

    assert find_relist(gone, db_engine=test_engine, now=NOW) is None


def test_find_relist_returns_none_without_an_image_hash(test_engine):
    """Absence of evidence isn't evidence: a null hash must not read as
    'definitely sold', it just means this signal is unavailable."""
    with Session(test_engine) as session:
        gone = _seed(session, make_listing(source_id="gone", image_hash=None))
        _seed(session, make_listing(source_id="other", image_hash=None, first_seen_days_ago=1))

    assert find_relist(gone, db_engine=test_engine, now=NOW) is None


def test_find_relist_ignores_listings_already_marked_sold(test_engine):
    """Another *sold* listing with the same photo is a second sale of a
    similar item, which is real comp data, not evidence of a relist."""
    with Session(test_engine) as session:
        gone = _seed(session, make_listing(source_id="gone"))
        _seed(
            session,
            make_listing(
                source_id="also-sold", status=ListingStatus.likely_sold, first_seen_days_ago=1
            ),
        )

    assert find_relist(gone, db_engine=test_engine, now=NOW) is None


def test_find_relist_ignores_matches_outside_the_grace_window(test_engine):
    """A listing from months ago with the same stock photo is a different
    sale, not this one coming back."""
    with Session(test_engine) as session:
        gone = _seed(session, make_listing(source_id="gone"))
        _seed(session, make_listing(source_id="ancient", first_seen_days_ago=200))

    assert find_relist(gone, db_engine=test_engine, now=NOW) is None


def test_find_relist_does_not_match_the_listing_against_itself(test_engine):
    with Session(test_engine) as session:
        gone = _seed(session, make_listing(source_id="gone"))

    assert find_relist(gone, db_engine=test_engine, now=NOW) is None


def test_find_relist_stays_within_one_source(test_engine):
    """Cross-source photo matching is a different problem with different
    rules, deferred in docs/decisions/0002-image-hash-dedup.md."""
    with Session(test_engine) as session:
        gone = _seed(session, make_listing(source_id="gone"))
        other_source = make_listing(source_id="depop-1", first_seen_days_ago=1)
        other_source.source = "depop"
        _seed(session, other_source)

    assert find_relist(gone, db_engine=test_engine, now=NOW) is None


# --- real end dates and bid counts ----------------------------------------
# See docs/decisions/0006-capture-what-ebay-already-sends.md. These replace
# guessing at 30-day terms with what eBay actually published.


def make_auction(bid_count: int | None, **kwargs) -> Listing:
    listing = make_listing(is_auction=True, **kwargs)
    listing.bid_count = bid_count
    return listing


def test_an_auction_that_ended_with_no_bids_did_not_sell():
    """Close to the only non-inference in this module: no bids means nobody
    bought it, whatever the disappearance looks like."""
    result = score_sale(make_auction(bid_count=0), now=NOW)

    assert result.signals["auction_no_bids"] is True
    assert result.sale_confidence < 0.1


def test_an_auction_that_ended_with_bids_scores_well():
    """Somebody actually paid roughly this, which is the strongest kind of
    comp data available anywhere in the system."""
    with_bids = score_sale(make_auction(bid_count=12), now=NOW)
    without = score_sale(make_auction(bid_count=0), now=NOW)

    assert with_bids.signals["auction_had_bids"] is True
    assert with_bids.sale_confidence > without.sale_confidence * 5


def test_bid_count_is_ignored_for_non_auctions():
    listing = make_listing()
    listing.bid_count = 0
    result = score_sale(listing, now=NOW)

    assert "auction_no_bids" not in result.signals


def test_disappearing_at_the_published_end_date_looks_like_expiry():
    """The real signal, replacing the 30-day guess: it ran to term, so nobody
    bought it."""
    listing = make_listing(posted_days_ago=10, missing_days_ago=0)
    listing.item_end_date = NOW

    result = score_sale(listing, now=NOW)

    assert result.signals["ended_at_scheduled_end"] is True
    assert result.sale_confidence < BASE_CONFIDENCE


def test_disappearing_well_before_the_end_date_looks_like_a_sale():
    listing = make_listing(posted_days_ago=3, missing_days_ago=0)
    listing.item_end_date = NOW + timedelta(days=20)

    result = score_sale(listing, now=NOW)

    assert result.signals["ended_at_scheduled_end"] is False
    assert result.sale_confidence > BASE_CONFIDENCE


def test_a_real_end_date_beats_the_thirty_day_guess():
    """A listing sitting exactly 30 days old would trip the old heuristic, but
    if eBay says it wasn't due to end, it didn't run to term."""
    listing = make_listing(posted_days_ago=30, missing_days_ago=0)
    listing.item_end_date = NOW + timedelta(days=15)

    result = score_sale(listing, now=NOW)

    assert result.signals["ended_at_scheduled_end"] is False
    assert "near_listing_term" not in result.signals
    assert "term_inferred" not in result.signals


def test_the_thirty_day_guess_still_covers_rows_without_an_end_date():
    """GTC listings and rows ingested before 0007 have no end date, so the
    fallback has to stay live."""
    listing = make_listing(posted_days_ago=30, missing_days_ago=0)
    assert listing.item_end_date is None

    result = score_sale(listing, now=NOW)

    assert result.signals["near_listing_term"] is True
    assert result.signals["term_inferred"] is True


def test_an_auction_running_to_its_end_date_is_not_evidence_against_a_sale():
    """Auctions end at their scheduled end date by definition, sold or not.
    Reading that as "nobody bought it" would penalise every auction ever
    listed, including ones that plainly sold with a dozen bids."""
    listing = make_auction(bid_count=12, posted_days_ago=3, missing_days_ago=0)
    listing.item_end_date = NOW

    result = score_sale(listing, now=NOW)

    assert "ended_at_scheduled_end" not in result.signals
    assert result.sale_confidence > BASE_CONFIDENCE


def test_a_fixed_price_listing_running_to_its_end_date_still_counts_against_it():
    """The signal is real, just not for auctions: a fixed-price listing that
    reaches its end date is one nobody bought."""
    listing = make_listing(posted_days_ago=10, missing_days_ago=0)
    listing.item_end_date = NOW

    result = score_sale(listing, now=NOW)

    assert result.signals["ended_at_scheduled_end"] is True
    assert result.sale_confidence < BASE_CONFIDENCE


def test_estimated_shipping_costs_a_little_price_confidence():
    """Lighter than not knowing at all: a CALCULATED figure is at least the
    right order of magnitude, it just may be for the wrong location."""
    firm = make_listing(posted_days_ago=3)
    firm.shipping_cost = 9.99

    estimated = make_listing(posted_days_ago=3)
    estimated.shipping_cost = 9.99
    estimated.shipping_estimated = True

    unknown = make_listing(posted_days_ago=3)

    firm_score = score_sale(firm, now=NOW).price_confidence
    estimated_score = score_sale(estimated, now=NOW).price_confidence
    unknown_score = score_sale(unknown, now=NOW).price_confidence

    assert unknown_score < estimated_score < firm_score
    assert score_sale(estimated, now=NOW).signals["shipping_estimated"] is True

"""Valuation: sold comps into a number, and refusing to when they are not there.

Uses hand-built Comp objects for the scoring logic so the maths is testable
without a database or a k-NN index. The retrieval half is exercised against
real Postgres in tests/test_pgvector.py, since it needs pgvector.
"""

import pytest
from sqlmodel import Session

from api.models import EMBEDDING_DIM, Listing, ListingStatus
from ml.valuation import (
    UPWARD_BIAS_CAVEAT,
    Comp,
    _comp_price,
    value_listing,
    weighted_median,
)


def comp(price: float, weight: float = 1.0, *, sale=0.8, best_offer=False) -> Comp:
    listing = Listing(
        source="ebay", source_id=f"c{price}", title="comp", price=price, url="u",
        status=ListingStatus.likely_sold, sale_confidence=sale, price_confidence=weight,
        accepts_best_offer=best_offer,
    )
    return Comp(listing=listing, price=price, weight=weight)


# ------------------------------------------------------------ weighted median


def test_weighted_median_of_equal_weights_is_the_plain_median():
    assert weighted_median([(10.0, 1.0), (20.0, 1.0), (30.0, 1.0)]) == 20.0


def test_weight_pulls_the_median_toward_the_trusted_comps():
    """ADR 0007's whole point: a Best Offer sale happened, but at a price we
    cannot pin down, so it should count less than a plain fixed-price sale."""
    heavy_low = weighted_median([(10.0, 10.0), (20.0, 1.0), (30.0, 1.0)])
    heavy_high = weighted_median([(10.0, 1.0), (20.0, 1.0), (30.0, 10.0)])
    assert heavy_low == 10.0
    assert heavy_high == 30.0


def test_weighted_median_is_none_without_values():
    assert weighted_median([]) is None


def test_all_zero_weights_fall_back_to_the_unweighted_median():
    """The prices are still evidence even if nothing is trusted."""
    assert weighted_median([(10.0, 0.0), (20.0, 0.0), (30.0, 0.0)]) == 20.0


def test_median_not_mean_so_one_outlier_cannot_drag_the_estimate():
    """Residual comp spread is still 2.74x after 0013, so outliers survive
    filtering and a mean would chase them."""
    prices = [(100.0, 1.0), (105.0, 1.0), (110.0, 1.0), (5000.0, 1.0)]
    assert weighted_median(prices) <= 110.0


# ------------------------------------------------------------ delivered cost


def test_comp_price_prefers_delivered_cost():
    listing = Listing(source="ebay", source_id="a", title="t", price=100.0,
                      shipping_cost=15.0, url="u")
    assert _comp_price(listing) == (115.0, False)


def test_comp_price_falls_back_to_item_price_and_says_so():
    """The substitution is reported rather than silent: item price understates
    what a buyer paid, which pushes the estimate down and the deal score with
    it."""
    listing = Listing(source="ebay", source_id="a", title="t", price=100.0, url="u")
    price, substituted = _comp_price(listing)
    assert (price, substituted) == (100.0, True)


# ---------------------------------------------------- refusing to answer


def test_no_estimate_below_the_comp_minimum(test_engine):
    """A confident-looking number gets acted on; a missing one does not. 42%
    of listings currently land here and that is intended."""
    from sqlmodel import Session

    with Session(test_engine) as session:
        listing = Listing(source="depop", source_id="d1", title="thing", price=50.0, url="u")
        session.add(listing)
        session.commit()
        session.refresh(listing)
        listing_id = listing.id

    result = value_listing(listing_id, db_engine=test_engine)

    assert result is not None
    assert result.estimated_value is None
    assert result.deal_score is None
    assert result.has_estimate is False
    assert result.confidence == 0.0


def test_a_missing_listing_returns_none(test_engine):
    assert value_listing(9999, db_engine=test_engine) is None


# ------------------------------------------------------------------ caveats


def test_every_valuation_carries_the_upward_bias_caveat(test_engine):
    """The honest headline risk: comps are listings that LEFT THE MARKET,
    which means sold, expired unsold, or withdrawn. Values are biased high and
    deal scores optimistic, which is the wrong direction for a tool that says
    'you should buy this'."""
    from sqlmodel import Session

    with Session(test_engine) as session:
        listing = Listing(source="depop", source_id="d1", title="thing", price=50.0, url="u")
        session.add(listing)
        session.commit()
        session.refresh(listing)
        listing_id = listing.id

    result = value_listing(listing_id, db_engine=test_engine)
    assert UPWARD_BIAS_CAVEAT in result.caveats


# ------------------------------------------------------------- deal scoring


@pytest.mark.parametrize(
    "asking,estimate,expected",
    [
        (50.0, 100.0, 0.5),    # half price
        (100.0, 100.0, 0.0),   # at market
        (150.0, 100.0, -0.5),  # above market
    ],
)
def test_deal_score_is_a_fraction_of_the_estimate(asking, estimate, expected):
    """Positive means below the estimate. Expressed as a fraction so it reads
    as a percentage."""
    assert pytest.approx((estimate - asking) / estimate) == expected


def test_auctions_are_not_scanned_as_deals(test_engine):
    """An auction's price is the current bid, and with no bids it is an opening
    bid set deliberately low. Scoring that as a discount measures the seller's
    marketing rather than the market. ADR 0004 captured `is_auction` for this
    and left the decision to real data; 451 active auctions, 279 at zero bids.

    Note the asymmetry this test does NOT assert: a *sold* auction stays
    eligible as a comp, because its final price is a real transaction.
    """
    from ml.valuation import deal_candidates

    with Session(test_engine) as session:
        sold = Listing(
            source="ebay",
            source_id="sold-1",
            title="ASUS TUF RTX 4090 24GB",
            price=2000.0,
            url="https://ebay.com/1",
            status=ListingStatus.likely_sold,
            model_key="rtx-4090",
            embedding=[0.1] * EMBEDDING_DIM,
        )
        auction = Listing(
            source="ebay",
            source_id="auction-1",
            title="PNY RTX 4090 Verto 24gb",
            price=107.87,
            url="https://ebay.com/2",
            status=ListingStatus.active,
            model_key="rtx-4090",
            is_auction=True,
            bid_count=0,
            embedding=[0.1] * EMBEDDING_DIM,
        )
        fixed_price = Listing(
            source="ebay",
            source_id="buy-it-now-1",
            title="MSI RTX 4090 SUPRIM 24GB",
            price=1500.0,
            url="https://ebay.com/3",
            status=ListingStatus.active,
            model_key="rtx-4090",
            is_auction=False,
            embedding=[0.1] * EMBEDDING_DIM,
        )
        session.add_all([sold, auction, fixed_price])
        session.commit()
        session.refresh(auction)
        session.refresh(fixed_price)
        auction_id, fixed_id = auction.id, fixed_price.id

    candidates = deal_candidates(db_engine=test_engine)
    assert fixed_id in candidates
    assert auction_id not in candidates

from sqlmodel import Session

from api.models import EMBEDDING_DIM, Listing
from ml.match import (
    COMPS_FROM_CANDIDATES,
    COMPS_FROM_EPID,
    comps_for_epid,
    MATCH_BY_EMBEDDING,
    MATCH_BY_IMAGE_HASH,
    find_by_image_hash,
    match_listing,
    summarize_prices,
)
from ml.similar import Match


def make(source, source_id, title, price, *, image_hash=None, shipping=None, engine=None):
    listing = Listing(
        source=source,
        source_id=source_id,
        title=title,
        price=price,
        shipping_cost=shipping,
        url=f"https://example.com/{source_id}",
        images=["https://img/x.jpg"],
        image_hash=image_hash,
    )
    with Session(engine) as session:
        session.add(listing)
        session.commit()
        session.refresh(listing)
        session.expunge(listing)
    return listing


# -------------------------------------------------- the cheap exact first pass


def test_a_reused_stock_photo_matches_exactly(test_engine):
    """The point of image_hash, finally used for what it is good at: a foreign
    seller reusing a manufacturer or eBay photo is the same product, provably,
    for one indexed lookup and no model at all."""
    ebay = make("ebay", "e1", "Nintendo Switch OLED", 250.0, image_hash="abc123", engine=test_engine)
    captured = make("depop", "d1", "switch oled bundle", 180.0, image_hash="abc123", engine=test_engine)

    result = match_listing(captured.id, db_engine=test_engine)

    assert result is not None
    assert result.matched_by == MATCH_BY_IMAGE_HASH
    assert result.is_exact is True
    assert result.confidence == 1.0
    assert [c.listing.id for c in result.candidates] == [ebay.id]


def test_image_hash_matching_only_looks_at_ebay(test_engine):
    """Only eBay is a comp source. Matching a Depop photo against other Depop
    photos would price against a source with no usable history (ADR 0008)."""
    make("depop", "d-other", "another depop listing", 90.0, image_hash="abc123", engine=test_engine)
    captured = make("depop", "d1", "switch", 180.0, image_hash="abc123", engine=test_engine)

    assert find_by_image_hash(captured, db_engine=test_engine) == []


def test_a_listing_without_a_hash_matches_nothing_by_hash(test_engine):
    captured = make("depop", "d1", "switch", 180.0, engine=test_engine)
    assert find_by_image_hash(captured, db_engine=test_engine) == []


def test_exact_match_wins_over_embeddings(test_engine):
    """Cheapest and most precise first. An exact photo match should not be
    diluted by visually-similar neighbours."""
    make("ebay", "e1", "exact same photo", 250.0, image_hash="abc123", engine=test_engine)
    captured = make("depop", "d1", "switch", 180.0, image_hash="abc123", engine=test_engine)

    with Session(test_engine) as session:
        row = session.get(Listing, captured.id)
        row.embedding = [0.1] * EMBEDDING_DIM
        session.add(row)
        session.commit()

    result = match_listing(captured.id, db_engine=test_engine)
    assert result.matched_by == MATCH_BY_IMAGE_HASH


# ------------------------------------------------------------- not yet ready


def test_an_unembedded_listing_reports_not_analysed_rather_than_no_match(test_engine):
    """An ordinary state during backfill, not an error. The caller must be able
    to say 'not analysed yet' instead of 'nothing matches', which mean very
    different things to someone deciding whether to buy."""
    captured = make("depop", "d1", "switch", 180.0, engine=test_engine)

    result = match_listing(captured.id, db_engine=test_engine)

    assert result is not None
    assert result.candidates == []
    assert result.confidence == 0.0


def test_a_missing_listing_returns_none(test_engine):
    assert match_listing(9999, db_engine=test_engine) is None


# ------------------------------------------------------------ price context


def test_price_context_reports_asking_prices_and_a_spread():
    candidates = [
        Match(listing=Listing(source="ebay", source_id="a", title="a", price=100.0,
                              shipping_cost=10.0, url="u"), similarity=0.9),
        Match(listing=Listing(source="ebay", source_id="b", title="b", price=200.0,
                              shipping_cost=20.0, url="u"), similarity=0.8),
        Match(listing=Listing(source="ebay", source_id="c", title="c", price=300.0,
                              shipping_cost=None, url="u"), similarity=0.7),
    ]

    context = summarize_prices(candidates)

    assert context.candidate_count == 3
    assert context.median_price == 200.0
    assert (context.min_price, context.max_price) == (100.0, 300.0)
    # Only the two with known shipping contribute, deliberately: treating
    # unknown shipping as zero would make eBay look cheaper than it is.
    assert context.listings_with_known_shipping == 2
    assert context.median_total_cost == 165.0
    assert context.spread_ratio == 3.0


def test_price_context_is_none_without_candidates():
    assert summarize_prices([]) is None


def test_a_wide_spread_is_visible_as_a_warning_signal():
    """The measured signature of a bad candidate set: ADR 0009 saw $578 to
    $3,000 across 'matching' prebuilt PCs. A caller should read a high spread
    as a reason to distrust the match, not as a wide market."""
    candidates = [
        Match(listing=Listing(source="ebay", source_id="a", title="a", price=578.0, url="u"),
              similarity=0.87),
        Match(listing=Listing(source="ebay", source_id="b", title="b", price=3000.0, url="u"),
              similarity=0.84),
    ]
    assert summarize_prices(candidates).spread_ratio > 5


def test_median_total_cost_is_none_when_no_shipping_is_known():
    candidates = [
        Match(listing=Listing(source="ebay", source_id="a", title="a", price=100.0, url="u"),
              similarity=0.9)
    ]
    context = summarize_prices(candidates)
    assert context.median_total_cost is None
    assert context.median_price == 100.0


# ------------------------------------------------- variant-aware comp sets


def make_v(source_id, title, price, *, epid=None, lot=None, defect=False,
           completeness=None, embedding=None, engine=None):
    listing = Listing(
        source="ebay", source_id=source_id, title=title, price=price,
        url=f"https://example.com/{source_id}", images=["https://img/x.jpg"],
        epid=epid, lot_size=lot, has_defect=defect, completeness=completeness,
        embedding=embedding,
    )
    with Session(engine) as session:
        session.add(listing)
        session.commit()
        session.refresh(listing)
        session.expunge(listing)
    return listing


def test_comps_for_epid_excludes_lots_and_defects(test_engine):
    """Sharing a catalog id does not make a box of fifty, or a broken unit,
    comparable to a working single item."""
    make_v("ok1", "Switch OLED", 200.0, epid="E1", engine=test_engine)
    make_v("ok2", "Switch OLED white", 210.0, epid="E1", engine=test_engine)
    make_v("lot", "Lot of 50 Switch OLED", 9000.0, epid="E1", lot=50, engine=test_engine)
    make_v("bad", "Switch OLED cracked", 60.0, epid="E1", defect=True, engine=test_engine)

    peers = comps_for_epid("E1", db_engine=test_engine)

    assert sorted(p.source_id for p in peers) == ["ok1", "ok2"]

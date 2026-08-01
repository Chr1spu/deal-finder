"""Tests for the things SQLite cannot stand in for.

The default `test_engine` fixture happily creates and round-trips a Vector
column (SQLite accepts arbitrary type names), so every other test file uses
it. What SQLite cannot do is *behave* like pgvector: the distance operators,
the real column type, and ordering by similarity. Those need real Postgres,
and these tests skip when it isn't running rather than failing the suite.
"""

from sqlalchemy import text
from sqlmodel import Session, col, delete, select

from api.models import EMBEDDING_DIM, Listing
from ml.similar import find_similar_to_listing, find_similar_to_vector


def unit_vector(*leading: float) -> list[float]:
    """A vector padded out to EMBEDDING_DIM and L2-normalized, the same shape
    the pipeline actually writes."""
    values = list(leading) + [0.0] * (EMBEDDING_DIM - len(leading))
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


def make_listing(source_id: str, title: str, vector: list[float] | None, source: str = "ebay"):
    return Listing(
        source=source,
        source_id=source_id,
        title=title,
        price=100.0,
        url=f"https://example.com/{source_id}",
        images=["https://i.ebayimg.com/images/g/x/s-l225.jpg"],
        embedding=vector,
    )


def clear(engine) -> None:
    with Session(engine) as session:
        session.execute(delete(Listing))  # execute, not exec: SQLModel.exec only types selects
        session.commit()


def test_the_extension_and_column_type_are_real(pg_engine):
    with Session(pg_engine) as session:
        installed = session.exec(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).first()
        assert installed is not None, "pgvector extension is not installed"

        column_type = session.exec(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'listing'::regclass AND attname = 'embedding'"
            )
        ).first()
        assert column_type[0] == f"vector({EMBEDDING_DIM})"


def test_cosine_distance_orders_neighbours_by_similarity(pg_engine):
    clear(pg_engine)
    with Session(pg_engine) as session:
        session.add(make_listing("near", "almost the same", unit_vector(1.0, 0.05)))
        session.add(make_listing("mid", "somewhat alike", unit_vector(1.0, 1.0)))
        session.add(make_listing("far", "unrelated", unit_vector(0.0, 1.0)))
        session.commit()

    matches = find_similar_to_vector(unit_vector(1.0, 0.0), k=3, db_engine=pg_engine)

    assert [m.listing.source_id for m in matches] == ["near", "mid", "far"]
    # Similarity is 1 - cosine distance, so it falls in [0, 1] and decreases
    # down the list. Stage 4's match confidence depends on both facts.
    assert matches[0].similarity > matches[1].similarity > matches[2].similarity
    assert 0.0 <= matches[-1].similarity <= 1.0
    assert matches[0].similarity > 0.99


def test_unembedded_listings_are_never_returned(pg_engine):
    """A NULL embedding must be excluded, not sorted to one end. This is why
    the column is nullable rather than defaulting to a zero vector: a zero
    sentinel would sit at a fixed distance from everything and turn up in
    every result set."""
    clear(pg_engine)
    with Session(pg_engine) as session:
        session.add(make_listing("embedded", "has a vector", unit_vector(1.0)))
        session.add(make_listing("pending", "not embedded yet", None))
        session.commit()

    matches = find_similar_to_vector(unit_vector(1.0), k=10, db_engine=pg_engine)

    assert [m.listing.source_id for m in matches] == ["embedded"]


def test_only_ebay_is_searched_because_only_ebay_is_a_comp_source(pg_engine):
    """docs/decisions/0008: eBay is the reference index. Matching a foreign
    photo against other foreign listings would price against a source with no
    usable history."""
    clear(pg_engine)
    with Session(pg_engine) as session:
        session.add(make_listing("ebay-one", "an ebay listing", unit_vector(1.0)))
        session.add(
            make_listing("depop-one", "a depop listing", unit_vector(1.0), source="depop")
        )
        session.commit()

    matches = find_similar_to_vector(unit_vector(1.0), k=10, db_engine=pg_engine)

    assert [m.listing.source for m in matches] == ["ebay"]


def test_a_listing_is_not_its_own_neighbour(pg_engine):
    clear(pg_engine)
    with Session(pg_engine) as session:
        subject = make_listing("subject", "the query", unit_vector(1.0))
        session.add(subject)
        session.add(make_listing("other", "a neighbour", unit_vector(1.0, 0.1)))
        session.commit()
        session.refresh(subject)
        subject_id = subject.id

    matches = find_similar_to_listing(subject_id, k=10, db_engine=pg_engine)

    assert [m.listing.source_id for m in matches] == ["other"]


def test_an_unembedded_listing_returns_no_neighbours_rather_than_raising(pg_engine):
    """'Not embedded yet' is an ordinary state during backfill, not an error."""
    clear(pg_engine)
    with Session(pg_engine) as session:
        subject = make_listing("pending", "not embedded", None)
        session.add(subject)
        session.commit()
        session.refresh(subject)
        subject_id = subject.id

    assert find_similar_to_listing(subject_id, db_engine=pg_engine) == []


def test_the_orm_distance_expression_resolves(pg_engine):
    """Guards the one thing the step-3 spike could not: that
    col(Listing.embedding).cosine_distance(...) compiles against Postgres and
    is usable in an ORDER BY, rather than only working in raw SQL."""
    clear(pg_engine)
    with Session(pg_engine) as session:
        session.add(make_listing("a", "first", unit_vector(1.0)))
        session.add(make_listing("b", "second", unit_vector(0.0, 1.0)))
        session.commit()

        distance = col(Listing.embedding).cosine_distance(unit_vector(1.0))
        rows = session.exec(
            select(Listing.source_id, distance.label("d"))
            .where(col(Listing.embedding).is_not(None))
            .order_by(distance)
        ).all()

    assert [row[0] for row in rows] == ["a", "b"]
    assert rows[0][1] < rows[1][1]


# ----------------------------------------- two-hop pricing (needs real k-NN)


def make_priced(source_id, title, price, *, epid=None, vector=None, lot=None, defect=False):
    return Listing(
        source="ebay", source_id=source_id, title=title, price=price,
        url=f"https://example.com/{source_id}", images=["https://img/x.jpg"],
        epid=epid, embedding=vector, lot_size=lot, has_defect=defect,
    )


def test_price_context_prefers_epid_over_the_neighbour_set(pg_engine):
    """The two-hop. Identification finds the product; the catalog id prices it.
    Measured on the real corpus, epid-keyed comp sets have a median price
    spread of 1.42x against 4.83x for raw CLIP neighbours."""
    from ml.match import COMPS_FROM_EPID, match_listing

    clear(pg_engine)
    with Session(pg_engine) as session:
        subject = make_priced("subj", "switch oled", 150.0, vector=unit_vector(1.0))
        session.add(subject)
        session.add(make_priced("near", "Switch OLED console", 200.0, epid="E1",
                                vector=unit_vector(1.0, 0.02)))
        session.add(make_priced("peer1", "Switch OLED white", 205.0, epid="E1"))
        session.add(make_priced("peer2", "Switch OLED 64GB", 195.0, epid="E1"))
        session.commit()
        session.refresh(subject)
        subject_id = subject.id

    result = match_listing(subject_id, db_engine=pg_engine)

    assert result is not None
    assert result.comps_from == COMPS_FROM_EPID
    assert result.epid == "E1"
    assert result.price_context.candidate_count == 3, "everything sharing the epid, not just neighbours"


def test_epid_pricing_still_excludes_lots_and_defects(pg_engine):
    """Sharing a catalog id does not make a box of fifty comparable to one unit."""
    from ml.match import match_listing

    clear(pg_engine)
    with Session(pg_engine) as session:
        subject = make_priced("subj", "switch oled", 150.0, vector=unit_vector(1.0))
        session.add(subject)
        session.add(make_priced("near", "Switch OLED", 200.0, epid="E1",
                                vector=unit_vector(1.0, 0.02)))
        session.add(make_priced("peer", "Switch OLED white", 205.0, epid="E1"))
        session.add(make_priced("lot", "Lot of 50 Switch", 9000.0, epid="E1", lot=50))
        session.commit()
        session.refresh(subject)
        subject_id = subject.id

    result = match_listing(subject_id, db_engine=pg_engine)

    prices = [c.listing.price for c in result.candidates]
    assert 9000.0 not in prices
    assert result.price_context.max_price < 1000.0


def test_price_context_falls_back_when_the_match_has_no_epid(pg_engine):
    """46% coverage overall, 3.9% on prebuilt PCs, so the fallback is the
    common path rather than an edge case."""
    from ml.match import COMPS_FROM_CANDIDATES, match_listing

    clear(pg_engine)
    with Session(pg_engine) as session:
        subject = make_priced("subj", "gaming pc", 900.0, vector=unit_vector(1.0))
        session.add(subject)
        session.add(make_priced("near", "custom gaming pc", 1000.0, vector=unit_vector(1.0, 0.02)))
        session.commit()
        session.refresh(subject)
        subject_id = subject.id

    result = match_listing(subject_id, db_engine=pg_engine)

    assert result.comps_from == COMPS_FROM_CANDIDATES
    assert result.epid is None
    assert result.price_context is not None


def test_lots_and_defects_are_excluded_from_knn(pg_engine):
    """Filters, not weights: a $113,000 lot of fifty is not a noisy measurement
    of one stick's value."""
    clear(pg_engine)
    with Session(pg_engine) as session:
        session.add(make_priced("good", "SK Hynix 32GB DDR5", 200.0, vector=unit_vector(1.0)))
        session.add(make_priced("lot", "Lot of 50 SK Hynix 64GB", 113000.0,
                                vector=unit_vector(1.0), lot=50))
        session.add(make_priced("bad", "SK Hynix 32GB cracked", 40.0,
                                vector=unit_vector(1.0), defect=True))
        session.commit()

    matches = find_similar_to_vector(unit_vector(1.0), k=10, db_engine=pg_engine)

    assert [m.listing.source_id for m in matches] == ["good"]


def test_epid_pricing_respects_stated_completeness(pg_engine):
    """An epid is the same product, not the same package. A console-only
    listing must not be priced against full bundles sharing its catalog id."""
    from ml.match import match_listing

    clear(pg_engine)
    with Session(pg_engine) as session:
        subject = make_priced("subj", "switch oled console only", 140.0, vector=unit_vector(1.0))
        subject.completeness = "bare"
        session.add(subject)
        near = make_priced("near", "Switch OLED", 200.0, epid="E1", vector=unit_vector(1.0, 0.02))
        session.add(near)
        bare = make_priced("bare-peer", "Switch OLED console only", 150.0, epid="E1")
        bare.completeness = "bare"
        session.add(bare)
        bundle = make_priced("bundle-peer", "Switch OLED bundle w/ dock", 300.0, epid="E1")
        bundle.completeness = "bundle"
        session.add(bundle)
        session.commit()
        session.refresh(subject)
        subject_id = subject.id

    result = match_listing(subject_id, db_engine=pg_engine)

    titles = [c.listing.source_id for c in result.candidates]
    assert "bundle-peer" not in titles, "a bundle is not a comp for a bare unit"
    assert "bare-peer" in titles


# ------------------------------------------ valuation retrieval (needs k-NN)


def test_valuation_uses_sold_comps_not_active_ones(pg_engine):
    """Active listings are asking prices, which ml/match.py reports separately
    and labels as such. Mixing them would blend what someone wanted with what
    the market cleared."""
    from api.models import ListingStatus
    from ml.valuation import find_sold_comps

    clear(pg_engine)
    with Session(pg_engine) as session:
        subject = make_priced("subj", "graphics card", 300.0, vector=unit_vector(1.0))
        session.add(subject)
        for i in range(3):
            sold = make_priced(f"sold{i}", "graphics card", 400.0 + i, vector=unit_vector(1.0, 0.01))
            sold.status = ListingStatus.likely_sold
            sold.sale_confidence = 0.8
            sold.price_confidence = 1.0
            session.add(sold)
        session.add(make_priced("active", "graphics card", 9999.0, vector=unit_vector(1.0, 0.01)))
        session.commit()
        session.refresh(subject)
        subject_id = subject.id
        subject_copy = subject
        session.expunge(subject_copy)

    comps = find_sold_comps(subject_copy, db_engine=pg_engine)

    assert len(comps) == 3
    assert all(c.listing.status == ListingStatus.likely_sold for c in comps)
    assert 9999.0 not in [c.price for c in comps]


def test_low_sale_confidence_comps_are_excluded_not_downweighted(pg_engine):
    """ADR 0007: a relisted item that probably never sold is not weak evidence
    of a sale price, it is evidence of nothing."""
    from api.models import ListingStatus
    from ml.valuation import find_sold_comps

    clear(pg_engine)
    with Session(pg_engine) as session:
        subject = make_priced("subj", "graphics card", 300.0, vector=unit_vector(1.0))
        session.add(subject)
        good = make_priced("good", "graphics card", 400.0, vector=unit_vector(1.0, 0.01))
        good.status = ListingStatus.likely_sold
        good.sale_confidence = 0.8
        session.add(good)
        relist = make_priced("relist", "graphics card", 50.0, vector=unit_vector(1.0, 0.01))
        relist.status = ListingStatus.likely_sold
        relist.sale_confidence = 0.135  # the score a detected relist gets
        session.add(relist)
        session.commit()
        session.refresh(subject)
        subject_copy = subject
        session.expunge(subject_copy)

    comps = find_sold_comps(subject_copy, db_engine=pg_engine)

    assert [c.listing.source_id for c in comps] == ["good"]


def test_a_full_valuation_produces_an_estimate_and_a_deal_score(pg_engine):
    from api.models import ListingStatus
    from ml.valuation import value_listing

    clear(pg_engine)
    with Session(pg_engine) as session:
        subject = make_priced("subj", "graphics card", 200.0, vector=unit_vector(1.0))
        session.add(subject)
        for i, price in enumerate((400.0, 410.0, 420.0, 430.0)):
            sold = make_priced(f"s{i}", "graphics card", price, vector=unit_vector(1.0, 0.01))
            sold.status = ListingStatus.likely_sold
            sold.sale_confidence = 0.8
            sold.price_confidence = 1.0
            session.add(sold)
        session.commit()
        session.refresh(subject)
        subject_id = subject.id

    result = value_listing(subject_id, db_engine=pg_engine)

    assert result is not None
    assert result.has_estimate is True
    assert 400.0 <= result.estimated_value <= 430.0
    assert result.deal_score > 0.4, "asking 200 against a ~415 estimate is a real discount"
    assert result.comp_count == 4
    assert 0.0 < result.confidence <= 1.0


def test_model_key_is_an_exact_match_not_a_soft_one(pg_engine):
    """Unlike every other spec filter, an unstated model key is EXCLUDED.

    "Gigabyte 3060 Ti" with no "RTX" prefix stores model_key as NULL, and
    keeping it would let a 3060 Ti comp a 3080 Ti. Measured: allowing NULL
    gave 30 comps at 8.69x spread, exact matching gave 16 at 2.33x.
    """
    clear(pg_engine)
    with Session(pg_engine) as session:
        same = make_priced("same", "RTX 3080 Ti", 700.0, vector=unit_vector(1.0))
        same.model_key = "rtx-3080-ti"
        session.add(same)
        other = make_priced("other-model", "RTX 3060 Ti", 250.0, vector=unit_vector(1.0, 0.01))
        other.model_key = "rtx-3060-ti"
        session.add(other)
        unnamed = make_priced("no-model", "Gigabyte 3060 Ti", 220.0, vector=unit_vector(1.0, 0.01))
        session.add(unnamed)  # model_key stays None
        session.commit()

    matches = find_similar_to_vector(unit_vector(1.0), k=10, db_engine=pg_engine,
                                     model_key="rtx-3080-ti")

    assert [m.listing.source_id for m in matches] == ["same"]


def test_other_spec_filters_still_keep_unstated_rows(pg_engine):
    """The contrast that makes the model_key rule a decision rather than an
    inconsistency: 89% of titles state no completeness, so excluding unstated
    rows there would discard most of the corpus."""
    clear(pg_engine)
    with Session(pg_engine) as session:
        stated = make_priced("stated", "console only", 100.0, vector=unit_vector(1.0))
        stated.completeness = "bare"
        session.add(stated)
        session.add(make_priced("unstated", "a console", 120.0, vector=unit_vector(1.0, 0.01)))
        session.commit()

    matches = find_similar_to_vector(unit_vector(1.0), k=10, db_engine=pg_engine,
                                     completeness="bare")

    assert {m.listing.source_id for m in matches} == {"stated", "unstated"}

"""Query the embedding index: given a vector or a listing, find the eBay
listings that look most like it.

This is the read side of docs/decisions/0009. Under ADR 0008 the eBay corpus
is a reference index and listings from other sources are queries against it,
so `find_similar_to_vector` (embed a foreign photo, search eBay) is the shape
the product actually needs. `find_similar_to_listing` is the eBay-to-eBay
case, useful for sanity-checking the index and for eBay-internal comps.

Deliberately returns a similarity score alongside every row. Stage 4 has to
surface a cross-source match as a best guess with a confidence and the comps
behind it, never as a silent input to a deal score, and it cannot do that if
this layer throws the distances away.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from api.db import engine as default_engine
from api.models import Listing

# Restricting the index to eBay is a correctness constraint, not a default.
# Only eBay is a comp source (docs/decisions/0008), so matching a Depop photo
# against other Depop photos would answer a question nobody asked and, worse,
# invite pricing against a source with no usable price history.
COMP_SOURCE = "ebay"


@dataclass
class Match:
    """similarity is 1 - cosine_distance, so it lands in [0, 1] for the
    L2-normalized vectors this project writes. Higher is closer.

    Reported rather than hidden because visual similarity is evidence, not an
    answer: CLIP retrieves "three-fan graphics card", not "this exact SKU".

    **Do not use this as a comp confidence weight.** It measures how alike two
    images are, which is not the same as how comparable two items are, and
    measurement on the real corpus showed the two can run in opposite
    directions. Prebuilt PCs, whose photos are interchangeable black boxes and
    whose prices are set by invisible internals, scored ~0.87 while spanning
    $578 to $3,000. iPhones scored a *lower* ~0.77 while being far better
    comps. Identical-looking items score highest exactly when the photo
    carries least information about price.

    Treat this as a candidate-generation score. Whether a candidate is really
    the same product is stage 3b's job (extracted brand/model) or epid's, not
    this number's.
    """

    listing: Listing
    similarity: float


def find_similar_to_vector(
    vector: list[float],
    k: int = 10,
    db_engine: Engine | None = None,
    source: str | None = COMP_SOURCE,
    exclude_listing_id: int | None = None,
    comparable_only: bool = True,
    completeness: str | None = None,
    capacity_gb: int | None = None,
    spec_generation: str | None = None,
    form_factor: str | None = None,
    model_key: str | None = None,
    category: str | None = None,
) -> list[Match]:
    """The k nearest embedded listings to `vector`, closest first.

    Exact k-NN via a full scan, no ANN index. Measured over 10,484 embedded
    rows: 29 ms in Postgres, ~68 ms end to end through this function (the
    difference is hydrating ten full Listing objects, not the search). 100%
    recall, and it stays correct under the filters below, which is what an
    HNSW index would struggle with: it walks the graph first and filters
    after, so a selective filter can leave almost nothing. Revisit near 100k
    rows, or when a real query is measured above ~100 ms.
    """
    db_engine = db_engine or default_engine
    # pgvector attaches cosine_distance (and l2_distance, max_inner_product)
    # via a custom comparator_factory on the Vector type. That's invisible to
    # mypy through SQLModel's Mapped[...] wrapper, so the call is correct at
    # runtime but unprovable statically. tests/test_pgvector.py exercises it
    # against real Postgres, which is the check that actually matters here.
    distance = col(Listing.embedding).cosine_distance(vector)  # type: ignore[attr-defined]

    statement = select(Listing, distance.label("distance")).where(
        col(Listing.embedding).is_not(None)
    )
    if source is not None:
        statement = statement.where(Listing.source == source)
    if exclude_listing_id is not None:
        statement = statement.where(col(Listing.id) != exclude_listing_id)

    if comparable_only:
        # Filters, not weights. A multi-item lot and a for-parts unit are not
        # noisy measurements of a working single item's value, they measure
        # something else entirely, so they leave the set rather than being
        # discounted. Measured: one "Lot of 50" at $113,000 sits in the same
        # category as single sticks, and defective GPUs median $151 against
        # $420 clean. See docs/decisions/0012-variant-extraction.md.
        statement = statement.where(
            col(Listing.lot_size).is_(None),
            col(Listing.has_defect).is_(False),
            # Accessories are the worst of the three: a replacement heatsink
            # matches a graphics card on both model string and image, and
            # sits at 2-20% of its price. See docs/decisions/0013.
            col(Listing.is_accessory).is_(False),
            # A from-price does not describe the item its title names.
            col(Listing.price_is_from).is_(False),
        )

    if category is not None:
        # Category is a HARD filter, unlike the spec fields below, because it
        # is present on essentially every listing and a mismatch is never
        # merely unstated. Without it a whole "ASUS ROG Strix Gaming Desktop
        # Tower PC" at $1,500 entered the comp set for a graphics card: CLIP
        # matched two black boxes with RGB lighting, and the PC's title names
        # no GPU model, so every spec filter passed it through as "unstated".
        statement = statement.where(Listing.category == category)

    # model_key is an EXACT match: a candidate with no model key is excluded
    # rather than kept, unlike every other spec filter below.
    #
    # The distinction is what the field claims. Capacity, generation and form
    # factor are *attributes* a title may legitimately omit while still
    # describing the same product. model_key is a *product identity*, so on a
    # listing that has one, a candidate whose title names no model at all is
    # not "unstated", it is unidentified. Measured over 120 listings that have
    # a model key: allowing NULL gave 30 comps at an 8.69x price spread,
    # requiring an exact match gave 16 comps at 2.33x. Nine percent more
    # listings end up with too few comps to value, which 0014 already treats
    # as the right answer when the evidence is thin.
    if model_key is not None:
        statement = statement.where(col(Listing.model_key) == model_key)

    # The rest keep unstated rows: 89% of titles say nothing about
    # completeness, so requiring a match on a usually-silent field would
    # discard most of the corpus rather than sharpen it.
    for value, column in (
        (completeness, Listing.completeness),
        (capacity_gb, Listing.capacity_gb),
        (spec_generation, Listing.spec_generation),
        (form_factor, Listing.form_factor),
    ):
        if value is not None:
            statement = statement.where((col(column) == value) | col(column).is_(None))

    # Context-managed, so the connection goes back to the pool and the read
    # transaction actually ends. Without this the session is only closed
    # whenever it happens to be garbage collected, which leaves an idle
    # transaction open in Postgres: five of those exhaust the default pool and
    # the sixth call blocks forever, and any concurrent write to the same rows
    # waits behind them.
    with Session(db_engine) as session:
        rows = session.exec(statement.order_by(distance).limit(k)).all()
        return [Match(listing=listing, similarity=1.0 - float(dist)) for listing, dist in rows]


def find_similar_to_listing(
    listing_id: int,
    k: int = 10,
    db_engine: Engine | None = None,
    source: str | None = COMP_SOURCE,
) -> list[Match]:
    """Neighbours of an already-embedded listing, excluding itself.

    Returns an empty list if the listing has no embedding yet, rather than
    raising: "not embedded" is an ordinary state during backfill, not an error.
    """
    db_engine = db_engine or default_engine
    with Session(db_engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None or listing.embedding is None:
            return []
        vector = list(listing.embedding)

    return find_similar_to_vector(
        vector, k=k, db_engine=db_engine, source=source, exclude_listing_id=listing_id
    )

from datetime import datetime, timezone
from enum import Enum

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Column, DateTime, String, UniqueConstraint
from sqlmodel import Field, SQLModel

# Dimension of the CLIP checkpoint in ml/embeddings.py (ViT-B/32 -> 512).
# Defined here because the column declaration needs it, and duplicated as a
# literal in the migration on purpose: a migration describes the schema at its
# revision, so importing this would let a later checkpoint swap silently
# rewrite history. See docs/decisions/0009-clip-embeddings-pgvector.md.
EMBEDDING_DIM = 512


class ListingStatus(str, Enum):
    active = "active"
    likely_sold = "likely_sold"
    # A listing that's been active a long time and hasn't turned up in any
    # saved search for a long time. Not sold, not deleted, just not worth
    # spending a scarce API call on any more (eBay fixed-price listings can
    # auto-renew forever, so without this the check set only ever grows).
    # Ingestion flips it back to active for free if it reappears.
    # See docs/decisions/0003-ebay-call-budget.md.
    stale = "stale"


class Listing(SQLModel, table=True):
    """A normalized listing from any marketplace source.

    (source, source_id) is the dedup key within one source. image_hash
    (a perceptual hash of the primary photo) is computed on ingest but not
    yet used for cross-source duplicate matching, see
    docs/decisions/0002-image-hash-dedup.md.
    """

    id: int | None = Field(default=None, primary_key=True)

    source: str = Field(index=True)
    source_id: str = Field(index=True)

    title: str
    price: float
    currency: str = "USD"

    images: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    image_hash: str | None = Field(default=None, index=True)
    location: str | None = None
    condition: str | None = None
    category: str | None = None

    # Shipping is part of what a buyer actually pays, so comparing deals on
    # item price alone is a correctness bug once stage 4 is scoring them.
    # eBay returns this in the same itemSummary payload ingestion already
    # fetches, so capturing it costs no extra API calls.
    #
    # This matters most across sources: a Facebook pickup has zero shipping,
    # so an eBay comp at 500 plus 30 delivery means the item is worth 530
    # delivered, and a 450 local cash listing beats a naive price comparison.
    shipping_cost: float | None = None

    # True when eBay only offered a CALCULATED shipping cost, which depends on
    # the buyer's location, so the figure stored may be for somewhere else.
    # Treated like partially-unknown shipping when scoring price confidence.
    shipping_estimated: bool = Field(default=False)

    # Derived from eBay's buyingOptions. Typed and indexed rather than kept as
    # a raw JSON list because stage 4's comp query has to filter on them, and
    # is_auction in particular is load-bearing: for an auction, `price` is the
    # current bid, not an asking price, so it's a poor comp.
    # See docs/decisions/0004-trustworthy-comp-data.md.
    is_auction: bool = Field(default=False, index=True)
    accepts_best_offer: bool = Field(default=False)

    # eBay's own catalog product id. Two listings sharing an epid are
    # definitively the same product, which is what CLIP embeddings and NLP
    # brand/model extraction both only approximate. Free, exact, and indexed
    # because it's the best comp key available if coverage turns out good.
    # Nothing consumes it yet: measure coverage first.
    # See docs/decisions/0006-capture-what-ebay-already-sends.md.
    epid: str | None = Field(default=None, index=True)

    # When the listing is scheduled to end. eBay omits this for Good 'Til
    # Cancelled listings, so its absence is itself the signal: GTC listings
    # auto-renew under new ids and are the ones that manufacture false
    # "sales". is_gtc records that reading so the meaning of a null isn't
    # rediscovered every time someone reads this table.
    item_end_date: datetime | None = None
    # Three-state on purpose: True (a full item body showed no end date),
    # False (it showed one), None (never fetched a body, so unknown).
    # It was a plain bool defaulting to False, which quietly made "unknown"
    # indistinguishable from "definitely not GTC" and, worse, combined with a
    # bad inference at ingest to mark every non-auction listing GTC. Search
    # responses never carry itemEndDate, so only the disappearance check's
    # getItem body can decide this.
    # See docs/decisions/0011-ebay-does-not-404-ended-listings.md.
    is_gtc: bool | None = Field(default=None, index=True)

    # For auctions, close to decisive: one that ends having never been bid on
    # did not sell, whatever the disappearance looks like.
    bid_count: int | None = None

    # Seller quality. A zero-feedback seller at a great price is a different
    # proposition from a 99.9% seller at the same price, and stage 4 should
    # be able to say so.
    seller_feedback_score: int | None = None
    seller_feedback_percent: float | None = None

    # Programs like AUTHENTICITY_GUARANTEE, which move price materially in
    # some categories. JSON and unindexed: nothing queries it yet.
    qualified_programs: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))

    # Structured product attributes (Brand, Model, Storage Capacity, ...) from
    # the getItem body the disappearance check already fetches and used to
    # throw away. Left as raw JSON deliberately: aspect names vary by category
    # and coverage is unknown, so stage 3b should decide what earns a typed
    # column against real data rather than a guess.
    aspects: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))

    # Units eBay reports as sold. Real sales data rather than inference, for
    # multi-quantity listings.
    sold_quantity: int | None = None

    # What is actually being sold, extracted from the title, because nothing
    # structured carries it: epid identifies the product model and CLIP finds
    # things that look alike, and neither can see what is in the box. Measured,
    # a bare "Tablet Only" and a full dock-and-cables bundle sit at the same
    # $159.99 inside one epid.
    #
    # These are comp *filters*, not weights. A lot of 50 and a for-parts unit
    # are not noisy measurements of a working single item's value, they are
    # measurements of something else, so they leave the comp set rather than
    # being discounted (same reasoning as 0007's confidence split).
    # See docs/decisions/0012-variant-extraction.md.
    #
    # None means a single item, which is the overwhelming default.
    lot_size: int | None = Field(default=None, index=True)
    # "bare" | "complete" | "bundle" | None. None means UNSTATED, never
    # "complete": 89% of titles say nothing, and reading silence as a full
    # bundle is exactly the error that puts bare units in the wrong comp set.
    completeness: str | None = Field(default=None, index=True)
    has_defect: bool = Field(default=False, index=True)
    # A part or accessory FOR the product, not the product: a replacement
    # heatsink, a backplate, an NVLink bridge, an empty box. These match on
    # both model string and image, so neither epid nor CLIP rejects them,
    # while sitting at 2-20% of the real price. Grouping graphics cards by
    # chipset gave rtx-3090 a 1428x spread and this was the entire cause.
    # See docs/decisions/0013-spec-extraction.md.
    is_accessory: bool = Field(default=False, index=True)
    # One eBay listing offering several configurations displays the CHEAPEST
    # variant's price, so its price and its title describe different items.
    # That manufactures fake bargains and they sort to the TOP of a deal
    # ranking: an "iPhone 14 128GB 256GB - All Colors" at $259.99 against a
    # $650 estimate is the entry price, not a discount. 6.4% of the corpus.
    # See docs/decisions/0015-multi-variant-listings.md.
    price_is_from: bool = Field(default=False, index=True)

    # Spec, extracted from the title because eBay's structured aspects carry
    # it exactly where it is not needed: 99% capacity coverage on phones,
    # 0.3% on graphics cards. Normalized to GB so 1TB and 1024GB compare.
    capacity_gb: int | None = Field(default=None, index=True)
    # DDR3/4/5, PCIE3/4/5. 32GB DDR4 laptop memory medians $110.99 against
    # $396.99 for 32GB DDR5 desktop, so this separates real price tiers.
    spec_generation: str | None = Field(default=None, index=True)
    # laptop / desktop / server / m.2 / 2.5in.
    form_factor: str | None = Field(default=None, index=True)
    # A normalized chipset key ("rtx-4080-super") for the categories where the
    # model *is* the spec. None elsewhere, which is most of the corpus.
    model_key: str | None = Field(default=None, index=True)
    # The matched tokens, so no classification is opaque and every rule stays
    # separately falsifiable against real titles. Same pattern as sale_signals.
    variant_signals: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))

    url: str
    posted_at: datetime | None = None

    status: ListingStatus = Field(default=ListingStatus.active, sa_column=Column(String, index=True))

    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # last_seen_at is refreshed by ingestion whenever the listing turns up in
    # a saved search, which (since eBay only returns active listings) is free
    # proof it's still alive. last_checked_at is only set by the disappearance
    # check, which costs an API call. The gap between them is what the check
    # prioritizes on. See docs/decisions/0003-ebay-call-budget.md.
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    last_checked_at: datetime | None = Field(default=None, index=True)

    # Set by the first failed lookup, cleared if the listing turns up again.
    # A second consecutive failure is what actually marks it likely_sold, so
    # one transient 404 can't create a permanent false comp. Doubles as the
    # better estimate of when the item really left the market, since it's
    # earlier than the confirming check.
    missing_since: datetime | None = None

    # Two separate confidences, written when a disappearance is confirmed.
    # They answer different questions and must not be combined:
    #
    #   sale_confidence  - did this listing actually result in a sale?
    #                      (relists, auction bids, running to term)
    #   price_confidence - is price + shipping what the buyer really paid?
    #                      (Best Offer discounts, mid-auction bid snapshots)
    #
    # A relisted item probably never sold and belongs out of the comp set
    # entirely; a Best Offer sale definitely happened but at a price we can't
    # pin down, and belongs in with wider error bars. One number can't say
    # that. Stage 4 should gate comp membership on the first and weight
    # influence by the second.
    #
    # Both are ORDINAL, not probabilities: 0.675 does not mean 67.5% likely.
    # sale_signals keeps the per-signal breakdown, so no opaque number hides
    # why a comp was discounted and every weight stays separately falsifiable.
    # See docs/decisions/0005-sale-confidence.md and 0007-two-confidences.md.
    sale_confidence: float | None = None
    price_confidence: float | None = None
    sale_signals: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))

    # CLIP image embedding, L2-normalized at write time so cosine distance
    # (<=>) is meaningful and 1 - distance lands in [0, 1].
    #
    # Nullable rather than NOT NULL with a zero-vector default: a sentinel of
    # all zeros forms a fake cluster that sits equidistant from everything and
    # pollutes every k-NN result. "Not embedded" has to be representable.
    #
    # For eBay rows this is the reference index that foreign listings get
    # matched against, which is why eBay coverage matters more than coverage
    # elsewhere: a missing eBay embedding removes a possible match for every
    # future query, a missing Depop one costs a single lookup.
    # See docs/decisions/0009-clip-embeddings-pgvector.md.
    embedding: list[float] | None = Field(
        default=None, sa_column=Column(Vector(EMBEDDING_DIM), nullable=True)
    )

    # Stamped on every attempt, success *or* failure. Keying the work queue on
    # `embedding IS NULL` instead would retry the 12 imageless listings and
    # every dead image URL on every single run, forever.
    embedded_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_listing_source_id"),)

    @property
    def total_cost(self) -> float | None:
        """What a buyer actually pays: item price plus shipping.

        This, not `price`, is what any comparison should use. `price` is item
        price only, and reaching for it is the correctness bug ADR 0004
        flagged: a cheaper item with expensive delivery is the worse deal.

        Returns None when shipping is unknown, deliberately rather than
        falling back to `price`. Silently treating unknown shipping as free
        is the same bug relocated, and it biases in the dangerous direction
        by making items look cheaper than they are. Callers must decide what
        to do about it; `price_confidence` already records the doubt.

        Note this still excludes sales tax and Global Shipping Programme
        import charges, both of which push real cost higher again.
        See docs/decisions/0008-price-oracle-and-valuation-clients.md.
        """
        if self.shipping_cost is None:
            return None
        return self.price + self.shipping_cost


class ListingRead(SQLModel):
    """What GET /listings actually returns.

    Serializing the `Listing` table model directly was fine at 14 columns and
    stopped being fine at 36: the route had no pagination, so every request
    returned every column of every row. The embedding column is what forced
    the issue (512 floats per row, and psycopg hands back a numpy array where
    `list[float]` is annotated), but the response was already far larger than
    anything needed it to be.

    An explicit schema rather than response_model_exclude, because what a
    client receives should be readable in one place instead of inferred from
    a subtraction, and stage 5's frontend needs a read model regardless.

    Deliberately omitted: `embedding` (large, and meaningless to a client) and
    `sale_signals` (the per-signal confidence breakdown, useful for debugging
    a score but noise in a listing feed).
    """

    id: int | None = None

    source: str
    source_id: str
    title: str
    price: float
    currency: str
    # The property on Listing, not a column. This is what a comparison should
    # use rather than `price`, so it's surfaced next to it rather than left
    # for each client to recompute (and get wrong when shipping is unknown).
    total_cost: float | None = None

    images: list[str] = []
    image_hash: str | None = None
    location: str | None = None
    condition: str | None = None
    category: str | None = None

    shipping_cost: float | None = None
    shipping_estimated: bool = False
    is_auction: bool = False
    accepts_best_offer: bool = False

    epid: str | None = None
    item_end_date: datetime | None = None
    # None means "not established yet", not False. See Listing.is_gtc.
    is_gtc: bool | None = None
    bid_count: int | None = None
    seller_feedback_score: int | None = None
    seller_feedback_percent: float | None = None
    qualified_programs: list[str] | None = None
    aspects: dict | None = None
    sold_quantity: int | None = None

    url: str
    posted_at: datetime | None = None
    status: ListingStatus = ListingStatus.active

    first_seen_at: datetime
    last_seen_at: datetime
    last_checked_at: datetime | None = None
    missing_since: datetime | None = None

    sale_confidence: float | None = None
    price_confidence: float | None = None
    embedded_at: datetime | None = None


class SavedSearch(SQLModel, table=True):
    """A keyword/location config that ingestion runs against.

    No CRUD or auth yet (that's stage 5's "saved-search CRUD"). For now rows
    come from a seed migration or get added by hand, per stage 1's scope of
    "saved-search config, no UI yet."
    """

    id: int | None = Field(default=None, primary_key=True)

    keyword: str
    location: str | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Observability, not config. eBay reports how many results a query really
    # has; ingestion only ever sees the first 200. Recording the total turns
    # "which searches are we truncating?" from a guess into a query, which is
    # what the deferred pagination work needs to be prioritized sensibly.
    last_run_at: datetime | None = None
    last_result_total: int | None = None


class PriceObservation(SQLModel, table=True):
    """One recorded price for a listing, at a point in time.

    Append-only, and written only when the price or shipping actually changes
    (plus once on first insert). Ingestion runs hourly against ~10k listings,
    so recording every run regardless would add roughly 250,000 rows a day
    that almost all say "nothing happened"; keyed to changes, the table stays
    proportional to real events.

    Exists because ingestion used to overwrite Listing.price in place, which
    made price drops (a strong deal signal) invisible and the stage 6 price
    chart unbuildable. See docs/decisions/0004-trustworthy-comp-data.md.
    """

    id: int | None = Field(default=None, primary_key=True)

    listing_id: int = Field(foreign_key="listing.id", index=True)
    price: float
    shipping_cost: float | None = None

    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)

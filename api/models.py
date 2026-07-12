from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import JSON, Column, String, UniqueConstraint
from sqlmodel import Field, SQLModel


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
    shipping_cost: float | None = None

    # Derived from eBay's buyingOptions. Typed and indexed rather than kept as
    # a raw JSON list because stage 4's comp query has to filter on them, and
    # is_auction in particular is load-bearing: for an auction, `price` is the
    # current bid, not an asking price, so it's a poor comp.
    # See docs/decisions/0004-trustworthy-comp-data.md.
    is_auction: bool = Field(default=False, index=True)
    accepts_best_offer: bool = Field(default=False)

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

    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_listing_source_id"),)


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

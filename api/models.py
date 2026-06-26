from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import JSON, Column, String, UniqueConstraint
from sqlmodel import Field, SQLModel


class ListingStatus(str, Enum):
    active = "active"
    likely_sold = "likely_sold"


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

    url: str
    posted_at: datetime | None = None

    status: ListingStatus = Field(default=ListingStatus.active, sa_column=Column(String, index=True))

    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

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

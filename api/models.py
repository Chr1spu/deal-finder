from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import JSON, Column, String, UniqueConstraint
from sqlmodel import Field, SQLModel


class ListingStatus(str, Enum):
    active = "active"
    likely_sold = "likely_sold"


class Listing(SQLModel, table=True):
    """A normalized listing from any marketplace source.

    (source, source_id) is the dedup key. See PROJECT_PLAN.md stage 2 for
    the fuller dedup story once image-hash matching lands.
    """

    id: int | None = Field(default=None, primary_key=True)

    source: str = Field(index=True)
    source_id: str = Field(index=True)

    title: str
    price: float
    currency: str = "USD"

    images: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    location: str | None = None
    condition: str | None = None
    category: str | None = None

    url: str
    posted_at: datetime | None = None

    status: ListingStatus = Field(default=ListingStatus.active, sa_column=Column(String, index=True))

    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_listing_source_id"),)

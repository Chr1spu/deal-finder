"""Push-based capture: turn a listing the user is looking at into a Listing row.

Depop and Facebook Marketplace both arrive this way. Neither can be polled
server-side: Facebook's ToS forbids it and it is login-walled, and Depop now
returns 403 to every server-side request behind Cloudflare Bot Management,
including robots.txt (see docs/decisions/0010-depop-is-push-based-now.md).
So a browser extension running in the user's own session reads the page they
already have open and posts it here.

This module is the source-agnostic half: validation, normalization and
persistence. Per-site page parsing lives in the extension, in JavaScript,
because that is where the DOM is. The contract between them is CapturedListing.

Everything captured here is a *valuation client*, never a comp: these rows
have no reliable disappearance signal, so their prices can never become price
history. That is enforced structurally rather than by convention, since
"depop" and "facebook" appear in neither PULL_BASED_SOURCES nor COMP_SOURCES
in connectors/disappearance_check.py.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Engine
from sqlmodel import Session, select

from api.db import engine as default_engine
from api.models import Listing, PriceObservation
from connectors.image_hash import fetch_and_hash
from ml.extract import extract_variant

logger = logging.getLogger(__name__)

ImageHasher = Callable[[str], "str | None"]

# Sources that may be captured. A closed set rather than a free-text field:
# an extension bug sending source="Depop" or source="depop.com" would create a
# parallel universe of rows that no query filters on, and nothing would error.
CAPTURABLE_SOURCES: frozenset[str] = frozenset({"depop", "facebook"})


class CapturedListing(BaseModel):
    """One listing as the browser extension read it off the page.

    Deliberately forgiving about everything except the four fields that make a
    row meaningful (source, source_id, title, price). These pages are scraped
    from markup that changes without notice, so a capture that arrives with no
    size and no condition is still worth keeping, while one with no price is
    not a listing at all. Optional fields simply stay null and the valuation
    reports lower confidence.
    """

    source: str
    source_id: str
    title: str
    price: float
    currency: str = "USD"
    url: str

    images: list[str] = Field(default_factory=list)
    description: str | None = None
    condition: str | None = None
    size: str | None = None
    brand: str | None = None
    location: str | None = None
    seller: str | None = None
    # Depop and Facebook both show a shipping figure sometimes. None means
    # unknown, which is NOT the same as free, and Listing.total_cost already
    # refuses to conflate the two.
    shipping_cost: float | None = None

    @field_validator("source")
    @classmethod
    def _known_source(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in CAPTURABLE_SOURCES:
            raise ValueError(
                f"source must be one of {sorted(CAPTURABLE_SOURCES)}, got {value!r}. "
                "eBay is ingested through its official API, not captured."
            )
        return normalized

    @field_validator("price")
    @classmethod
    def _sane_price(cls, value: float) -> float:
        # A page-parsing failure usually shows up as 0 or a wild number rather
        # than as a missing field, so this catches the common breakage.
        if value <= 0:
            raise ValueError("price must be positive; a zero price means the page parse failed")
        return value

    @field_validator("source_id", "title", "url")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


def _attributes(captured: CapturedListing) -> dict | None:
    """Depop's structured fields (brand, size) go in the same `aspects` column
    eBay's localizedAspects land in, so stage 3b's matching reads one shape
    rather than branching per source."""
    attributes = {
        key: value
        for key, value in (
            ("Brand", captured.brand),
            ("Size", captured.size),
            ("Seller", captured.seller),
        )
        if value
    }
    return attributes or None


def to_listing(captured: CapturedListing, now: datetime | None = None) -> Listing:
    """Map a captured payload onto the shared Listing schema (not persisted).

    The equivalent of normalize_ebay_item for push sources, and deliberately
    the same target shape: one table, one set of downstream consumers. A
    captured listing differs only in which columns are null.
    """
    now = now or datetime.now(UTC)
    # Extract here rather than leaving it to the batch job: a capture is
    # matched immediately, and its own completeness is what decides whether
    # the comp set gets filtered. A Depop "console only" matched against
    # complete eBay bundles is precisely the error this guards.
    #
    # No category is passed, and a brand is NOT one. extract_variant reads
    # category as eBay's taxonomy ("Water Cooling", "Mixed Lots"), so a brand
    # in that slot can only misfire: "Snap-on Tools" contains an accessory
    # token and would flag the listing as an accessory.
    variant = extract_variant(captured.title, _attributes(captured))
    return Listing(
        source=captured.source,
        source_id=captured.source_id,
        title=captured.title,
        price=captured.price,
        currency=captured.currency,
        shipping_cost=captured.shipping_cost,
        images=captured.images,
        location=captured.location,
        condition=captured.condition,
        # Left null on purpose. `category` holds eBay's taxonomy, and
        # ml/similar.py filters candidates on it with `==`, no unstated
        # escape hatch, because on eBay it is always present. Storing a brand
        # here ("Nike") therefore matched zero eBay rows and every captured
        # listing with a brand retrieved no candidates at all: the one thing
        # the extension exists to do. A foreign listing has no eBay category,
        # and null is how this schema says "unstated". Brand travels in
        # aspects, where the matcher can read it without gating on it.
        category=None,
        aspects=_attributes(captured),
        url=captured.url,
        first_seen_at=now,
        last_seen_at=now,
        # Never GTC, never an auction: these are fixed-price listings on sites
        # with no auction mechanic. Set explicitly so the defaults aren't
        # mistaken for "unknown".
        is_gtc=False,
        is_auction=False,
        lot_size=variant.lot_size,
        completeness=variant.completeness,
        has_defect=variant.has_defect,
        is_accessory=variant.is_accessory,
        price_is_from=variant.price_is_from,
        capacity_gb=variant.capacity_gb,
        spec_generation=variant.spec_generation,
        form_factor=variant.form_factor,
        model_key=variant.model_key,
        variant_signals=variant.signals or {},
    )


def save_capture(
    captured: CapturedListing,
    db_engine: Engine | None = None,
    now: datetime | None = None,
    image_hasher: ImageHasher = fetch_and_hash,
) -> tuple[Listing, bool]:
    """Upsert a captured listing. Returns (listing, was_created).

    Re-capturing the same item is expected and useful rather than an error:
    it is the only freshness signal a push source has, since nothing polls
    these pages. A re-capture refreshes price and bumps last_seen_at, and
    records a PriceObservation when the price actually moved, which is the
    same treatment ingest gives an eBay listing it sees again.

    The primary image is perceptually hashed here, exactly as ingest does.
    Without it ml.match's cheap exact first pass is dead code for captured
    listings, which is precisely backwards: a foreign seller reusing a stock
    photo is the single highest-precision cross-source signal available, and
    reused photos are far more common off eBay than on it.
    """
    db_engine = db_engine or default_engine
    now = now or datetime.now(UTC)
    fresh = to_listing(captured, now=now)
    if fresh.images:
        fresh.image_hash = image_hasher(fresh.images[0])

    with Session(db_engine) as session:
        existing = session.exec(
            select(Listing).where(
                Listing.source == fresh.source, Listing.source_id == fresh.source_id
            )
        ).first()

        if existing is None:
            session.add(fresh)
            session.flush()  # assign an id for the observation below
            session.add(
                PriceObservation(
                    listing_id=fresh.id, price=fresh.price,
                    shipping_cost=fresh.shipping_cost, observed_at=now,
                )
            )
            session.commit()
            session.refresh(fresh)
            session.expunge(fresh)
            return fresh, True

        if (existing.price, existing.shipping_cost) != (fresh.price, fresh.shipping_cost):
            session.add(
                PriceObservation(
                    listing_id=existing.id, price=fresh.price,
                    shipping_cost=fresh.shipping_cost, observed_at=now,
                )
            )

        existing.price = fresh.price
        existing.currency = fresh.currency
        existing.title = fresh.title
        existing.last_seen_at = now
        if fresh.shipping_cost is not None:
            existing.shipping_cost = fresh.shipping_cost
        if fresh.aspects:
            existing.aspects = fresh.aspects
        # A re-capture may see better photos, and unlike eBay ingest there is
        # no image_hash relist detection on these rows to desync.
        if fresh.images and existing.images != fresh.images:
            existing.images = fresh.images
            existing.image_hash = fresh.image_hash
            existing.embedded_at = None  # re-embed against the new photo
            existing.embedding = None
        if existing.image_hash is None and fresh.image_hash is not None:
            existing.image_hash = fresh.image_hash

        session.add(existing)
        session.commit()
        session.refresh(existing)
        session.expunge(existing)
        return existing, False

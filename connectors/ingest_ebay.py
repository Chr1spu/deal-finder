"""Stage 1 ingestion entrypoint: runs every configured SavedSearch, normalizes
each result, and upserts. Run manually for now (`python -m connectors.ingest_ebay`);
gets wired into the Redis scheduler in stage 2.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial

import httpx
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from api.db import engine as default_engine
from api.models import Listing, ListingStatus, PriceObservation, SavedSearch
from connectors.ebay import EbayClient
from connectors.image_hash import fetch_and_hash
from connectors.normalizer import normalize_ebay_item
from ml.extract import extract_variant
from systems.preflight import assert_schema_current
from systems.ratelimit import QuotaExhaustedError

logger = logging.getLogger(__name__)

ImageHasher = Callable[[str], "str | None"]


def _refresh_from_summary(existing: Listing, fresh: Listing) -> None:
    """Copy every field an itemSummary carries from a freshly normalized
    listing onto the stored one, in place.

    This exists because the update path used to refresh six fields while the
    model grew to thirty-six. Anything added after stage 2 was therefore
    captured on insert and never again, so a listing already in the table
    could never acquire it: a whole corpus ingested before a column existed
    stays permanently blank even though every run since has seen the value.
    Keeping the field list in one function, rather than inline in the update
    branch, is what stops the next added column repeating that.

    Two rules that are easy to get backwards:

    Refresh, don't blank. A field missing from one response must not erase
    what an earlier response taught us, so most fields here only overwrite
    when the incoming value is not None. Same additive contract as
    normalizer.enrich_from_item_body. The exceptions are the ones that
    describe current state rather than accumulated knowledge (price, and the
    buying options that eBay always sends), which are meant to track reality
    and so are copied unconditionally.

    is_gtc is derived, not reported. It's inferred from an *absent* end date,
    so it can't be copied on its own: it's recomputed from the stored state
    after item_end_date settles, or a listing that gains a real end date
    keeps claiming to be Good 'Til Cancelled forever.
    """
    # Re-derived rather than copied, because a seller revising a title from
    # "Console" to "Console ONLY" changes what is being sold, and a stale
    # classification would quietly keep it in the wrong comp set.
    variant = extract_variant(fresh.title, existing.aspects, fresh.category)
    existing.title = fresh.title
    existing.lot_size = variant.lot_size
    existing.completeness = variant.completeness
    existing.has_defect = variant.has_defect
    existing.is_accessory = variant.is_accessory
    existing.price_is_from = variant.price_is_from
    existing.capacity_gb = variant.capacity_gb
    existing.spec_generation = variant.spec_generation
    existing.form_factor = variant.form_factor
    existing.model_key = variant.model_key
    existing.variant_signals = variant.signals or {}

    # Current state: always authoritative, always overwritten.
    existing.price = fresh.price
    existing.currency = fresh.currency
    existing.is_auction = fresh.is_auction
    existing.accepts_best_offer = fresh.accepts_best_offer

    # Shipping cost and its estimated flag move together: the flag describes
    # the cost, so refreshing one without the other can mark a firm price as
    # an estimate (or worse, the reverse).
    if fresh.shipping_cost is not None:
        existing.shipping_cost = fresh.shipping_cost
        existing.shipping_estimated = fresh.shipping_estimated

    if fresh.epid is not None:
        existing.epid = fresh.epid
    if fresh.bid_count is not None:
        existing.bid_count = fresh.bid_count
    if fresh.seller_feedback_score is not None:
        existing.seller_feedback_score = fresh.seller_feedback_score
    if fresh.seller_feedback_percent is not None:
        existing.seller_feedback_percent = fresh.seller_feedback_percent
    if fresh.qualified_programs is not None:
        existing.qualified_programs = fresh.qualified_programs

    # item_end_date and is_gtc are deliberately NOT touched here. Search
    # responses never carry itemEndDate, so `fresh` always has None for it and
    # copying that would erase a real value the disappearance check learned
    # from a full item body. Recomputing is_gtc from it is worse still: that
    # is the inference that marked every non-auction listing GTC.
    # Only enrich_from_item_body may set either.
    # See docs/decisions/0011-ebay-does-not-404-ended-listings.md.

    # Deliberately not refreshed: images (a changed photo would desync the
    # image_hash relist detection depends on, so that needs its own decision),
    # and aspects/sold_quantity (getItem-only, owned by enrich_from_item_body).


@dataclass
class IngestResult:
    """inserted and updated are tracked separately, rather than as one
    "upserted" total, because their ratio is the arrival rate: how many
    genuinely new listings show up per run. That's the number that determines
    whether the disappearance check's budget is sustainable as the corpus
    grows, and it was previously unmeasurable. See
    docs/decisions/0003-ebay-call-budget.md.
    """

    inserted: int = 0
    updated: int = 0
    reactivated: int = 0
    price_changes: int = 0
    truncated_searches: int = 0
    searches_run: int = 0
    searches_failed: int = 0
    quota_exhausted: bool = False

    @property
    def upserted(self) -> int:
        return self.inserted + self.updated

    def __iadd__(self, other: IngestResult) -> IngestResult:
        self.inserted += other.inserted
        self.updated += other.updated
        self.reactivated += other.reactivated
        self.price_changes += other.price_changes
        self.truncated_searches += other.truncated_searches
        self.searches_run += other.searches_run
        self.searches_failed += other.searches_failed
        self.quota_exhausted = self.quota_exhausted or other.quota_exhausted
        return self


def ingest_saved_search(
    saved_search: SavedSearch,
    client: EbayClient | None = None,
    db_engine: Engine | None = None,
    image_hasher: ImageHasher = fetch_and_hash,
) -> IngestResult:
    """client/db_engine are injectable so tests can run against a fake client
    and a throwaway DB instead of hitting eBay or Postgres. image_hasher is
    injectable too, so tests don't need real network access to hash images
    (see docs/decisions/0002-image-hash-dedup.md).

    saved_search.location isn't passed to eBay yet: the Browse API doesn't
    support free-text proximity search the way this schema implies (it only
    has country-level delivery/pickup filters). Revisit once a genuinely
    local source (Facebook Marketplace) needs it for real, see LEARNING_LOG.md.
    """
    client = client or EbayClient()
    db_engine = db_engine or default_engine
    search = client.search_items(saved_search.keyword)
    now = datetime.now(timezone.utc)

    result = IngestResult(searches_run=1)
    if search.total > len(search.items):
        result.truncated_searches = 1

    with Session(db_engine) as session:
        normalized = [normalize_ebay_item(raw) for raw in search.items]

        # One query for the whole page instead of one per item. At 200 results
        # x 64 searches that's the difference between ~12,800 round trips per
        # run and 64.
        source_ids = [listing.source_id for listing in normalized]
        existing_by_id = {
            row.source_id: row
            for row in session.exec(
                select(Listing).where(
                    Listing.source == "ebay", col(Listing.source_id).in_(source_ids)
                )
            ).all()
        }

        for listing in normalized:
            existing = existing_by_id.get(listing.source_id)

            if existing:
                # Capture what's stored, refresh, then compare: an observation
                # is written exactly when the stored values actually moved.
                # Comparing the incoming values instead would log a spurious
                # change every run for any listing whose shipping is absent
                # from a response, since _refresh_from_summary declines to
                # blank a known cost and the two would never agree again.
                #
                # Recording the old price at all matters because overwriting
                # in place used to make price drops (a strong deal signal)
                # invisible, and left the stage 6 price chart nothing to draw.
                # See docs/decisions/0004-trustworthy-comp-data.md.
                previous = (existing.price, existing.shipping_cost)
                _refresh_from_summary(existing, listing)

                if previous != (existing.price, existing.shipping_cost):
                    session.add(
                        PriceObservation(
                            listing_id=existing.id,
                            price=existing.price,
                            shipping_cost=existing.shipping_cost,
                            observed_at=now,
                        )
                    )
                    result.price_changes += 1

                existing.last_seen_at = listing.last_seen_at
                # Seen in a search means alive, so a listing that was queued
                # for disappearance confirmation is off the hook.
                existing.missing_since = None
                # Turning up in a search is proof the listing is live, so a
                # previously-retired one comes back for free. Deliberately
                # does NOT resurrect a likely_sold listing: that would undo a
                # confirmed sale (and its comp) on the strength of a search
                # hit, and eBay does relist sold items under new ids anyway.
                if existing.status == ListingStatus.stale:
                    existing.status = ListingStatus.active
                    result.reactivated += 1
                if existing.image_hash is None and existing.images:
                    existing.image_hash = image_hasher(existing.images[0])
                session.add(existing)
                result.updated += 1
            else:
                variant = extract_variant(listing.title, listing.aspects, listing.category)
                listing.lot_size = variant.lot_size
                listing.completeness = variant.completeness
                listing.has_defect = variant.has_defect
                listing.is_accessory = variant.is_accessory
                listing.price_is_from = variant.price_is_from
                listing.capacity_gb = variant.capacity_gb
                listing.spec_generation = variant.spec_generation
                listing.form_factor = variant.form_factor
                listing.model_key = variant.model_key
                listing.variant_signals = variant.signals or {}
                if listing.images:
                    listing.image_hash = image_hasher(listing.images[0])
                session.add(listing)
                session.flush()  # assign listing.id for the observation below
                session.add(
                    PriceObservation(
                        listing_id=listing.id,
                        price=listing.price,
                        shipping_cost=listing.shipping_cost,
                        observed_at=now,
                    )
                )
                result.inserted += 1

        # Same session, so a search that fails partway leaves neither listings
        # nor its own bookkeeping half-written.
        tracked = session.get(SavedSearch, saved_search.id)
        if tracked is not None:
            tracked.last_run_at = now
            tracked.last_result_total = search.total
            session.add(tracked)

        session.commit()

    return result


def ingest_all(
    client: EbayClient | None = None,
    db_engine: Engine | None = None,
    image_hasher: ImageHasher = fetch_and_hash,
) -> IngestResult:
    """Runs ingest_saved_search for every SavedSearch row in the DB. There's
    no saved-search UI or CRUD yet (that's stage 5); rows come from the seed
    migration or get added by hand for now.

    Each search is isolated: one failing search costs that search's results,
    not the whole run. Before stage 2.5 a single 429 on the first of 64
    searches aborted all of them, which is how a transient error turned into
    zero ingested listings.
    """
    db_engine = db_engine or default_engine
    # Before spending any quota: confirm this process is not running code
    # older than the database. A stale worker fails per-search inside the
    # isolation below, which reports success and ingests nothing.
    assert_schema_current(db_engine, Listing, SavedSearch)

    with Session(db_engine) as session:
        # Disabled searches cost nothing, which is the entire point of the
        # flag: each enabled search is one Browse call per run, forever. This
        # filter is the one place a bug would be invisible (the search simply
        # keeps running and quietly keeps spending), so a test asserts it.
        # See docs/decisions/0016-saved-search-crud.md.
        saved_searches = session.exec(
            select(SavedSearch).where(col(SavedSearch.enabled).is_(True))
        ).all()

    # One HTTP client for every image fetched across the whole run. The
    # module-level httpx.get() the hasher defaults to completes a fresh
    # TCP+TLS handshake per image, which across thousands of images is a
    # large part of why the first full ingest took 30-35 minutes.
    owns_http = image_hasher is fetch_and_hash
    http_client = httpx.Client(timeout=10.0) if owns_http else None
    if http_client is not None:
        image_hasher = partial(fetch_and_hash, http_client=http_client)

    total = IngestResult()
    try:
        for saved_search in saved_searches:
            try:
                total += ingest_saved_search(
                    saved_search, client=client, db_engine=db_engine, image_hasher=image_hasher
                )
            except QuotaExhaustedError:
                # Every remaining search would fail the same way, and each
                # attempt spends quota that's already gone. Stop, keep what
                # landed.
                logger.warning(
                    "eBay quota exhausted after %d searches, stopping ingest early",
                    total.searches_run,
                )
                total.quota_exhausted = True
                break
            except Exception:
                logger.exception("saved search %r failed, continuing", saved_search.keyword)
                total.searches_failed += 1
    finally:
        if http_client is not None:
            http_client.close()

    # Per-search isolation means a systematic fault looks like a successful
    # run that happened to find nothing. Say so loudly instead: if most
    # searches failed, something is wrong with the code or the database, not
    # with 60 separate eBay queries.
    if total.searches_run and total.searches_failed >= max(2, total.searches_run // 2):
        logger.error(
            "%d of %d searches failed. That is a systematic fault (stale worker code, "
            "schema drift, or credentials), not bad luck across independent queries.",
            total.searches_failed,
            total.searches_run,
        )

    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = datetime.now(timezone.utc)
    result = ingest_all()
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(
        f"Ran {result.searches_run} searches in {elapsed:.0f}s: "
        f"{result.inserted} new, {result.updated} updated, "
        f"{result.reactivated} reactivated, {result.price_changes} price changes, "
        f"{result.truncated_searches} searches hit the 200 cap, "
        f"{result.searches_failed} failed"
        + (" (stopped early: quota exhausted)" if result.quota_exhausted else "")
    )

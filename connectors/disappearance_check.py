"""Disappearance tracking: re-check active listings, mark ones that vanish
as likely_sold. This is how sold-price history gets built (see
PROJECT_PLAN.md section 1). Start running it early so data accumulates.

Generalized per source (stage 2, per docs/decisions/0001): each pull-based
source gets an entry in PULL_BASED_SOURCES, check_all_sources loops them
all. Push-based sources (Facebook) aren't checked here at all, see the ADR.

Stage 2.5 reshaped this from "check everything active" into "spend a scarce
API call only where there's genuine doubt", because eBay allows 5,000 Browse
calls/day and checking 10k listings four times a day wants 42,000. See
docs/decisions/0003-ebay-call-budget.md. Three ideas do the work:

  1. Ingestion refreshes last_seen_at whenever a listing turns up in a saved
     search, for free. eBay only returns *active* listings from search, so a
     recently-seen listing is provably alive and gets skipped entirely.
  2. Whatever's left is checked oldest-last_seen_at first (likeliest dead
     first), capped by a budget derived from the real remaining quota.
  3. Listings that never sell and never end get retired to `stale` so the
     candidate set stays bounded instead of growing forever.

Run manually for now (`python -m connectors.disappearance_check`); wired
into the RQ scheduler via systems/queue.py + systems/scheduler.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from api.db import engine as default_engine
from api.models import Listing, ListingStatus
from api.settings import settings
from connectors.ebay import EbayClient
from connectors.normalizer import enrich_from_item_body, listing_has_ended
from connectors.sale_confidence import find_relist, score_sale
from ml.extract import extract_variant
from systems.preflight import assert_schema_current
from systems.ratelimit import QuotaExhaustedError


class SourceClient(Protocol):
    def get_item(self, item_id: str) -> dict | None: ...


# Sources polled on a schedule. Depop will join this once it's built.
PULL_BASED_SOURCES: dict[str, type[SourceClient]] = {
    "ebay": EbayClient,
}

# Sources whose price history is trustworthy enough to value other listings
# against. Deliberately NOT the same set as PULL_BASED_SOURCES, and the two
# must not be merged: Depop gets polled, but only so its listings can be
# *scored* against eBay value, never to build history of its own. Item variety
# there is too high for per-item comps to mean anything, and mixing them into
# the pool would quietly contaminate the one source that does have the volume
# and catalog structure to support this.
# See docs/decisions/0008-price-oracle-and-valuation-clients.md.
COMP_SOURCES: frozenset[str] = frozenset({"ebay"})


@dataclass
class CheckResult:
    """Richer than the old (checked, marked_sold) tuple because a budgeted run
    has more ways to end than a full sweep did, and a run that stopped early
    on an exhausted quota needs to be distinguishable from one that simply
    found nothing to do."""

    checked: int = 0
    marked_sold: int = 0
    detected_relists: int = 0
    enriched: int = 0
    pending_confirmation: int = 0
    recovered: int = 0
    skipped_proven_alive: int = 0
    retired_stale: int = 0
    quota_exhausted: bool = False

    def __iadd__(self, other: CheckResult) -> CheckResult:
        self.checked += other.checked
        self.marked_sold += other.marked_sold
        self.detected_relists += other.detected_relists
        self.enriched += other.enriched
        self.pending_confirmation += other.pending_confirmation
        self.recovered += other.recovered
        self.skipped_proven_alive += other.skipped_proven_alive
        self.retired_stale += other.retired_stale
        self.quota_exhausted = self.quota_exhausted or other.quota_exhausted
        return self


SECONDS_PER_DAY = 86400

# What eBay actually grants this app, measured via getRateLimits rather than
# taken from docs. Used only to sanity-check configuration; the live remaining
# count from get_rate_limit() is what actually gates spending.
EBAY_DAILY_BROWSE_LIMIT = 5000


def estimate_daily_calls(saved_search_count: int) -> tuple[int, int]:
    """(ingest calls, check calls) per day under the current settings.

    The whole outage this design exists to prevent came from nobody doing this
    arithmetic: one get_item per active listing, four times a day, wanted
    roughly 42,000 calls against an allowance of 5,000. Keeping it as a
    function rather than a comment means a test can assert the configuration
    still fits, and adding 60 more saved searches fails loudly instead of
    silently exhausting the quota at 3am.
    """
    ingest_runs = SECONDS_PER_DAY / max(1, settings.ingest_interval_seconds)
    check_passes = SECONDS_PER_DAY / max(1, settings.disappearance_check_interval_seconds)
    ingest_calls = int(saved_search_count * ingest_runs)
    check_calls = int(settings.disappearance_check_budget * check_passes)
    return ingest_calls, check_calls


def resolve_budget(client: SourceClient, configured: int, reserve: int) -> int:
    """How many get_item calls this pass may actually spend.

    Asks eBay what's really left rather than trusting a hardcoded limit, then
    holds back `reserve` for ingestion, which must never be starved by the
    checker (ingestion is both the product and the cheapest liveness signal
    available, at 200 listings per call versus get_item's 1).

    Falls back to the configured budget when the real number is unavailable,
    since a client that can't report quota (a fake in tests, a future Depop
    client) should still be checkable.
    """
    get_rate_limit = getattr(client, "get_rate_limit", None)
    if get_rate_limit is None:
        return configured

    rate_limit = get_rate_limit()
    if rate_limit is None:
        return configured

    return max(0, min(configured, rate_limit.remaining - reserve))


def retire_stale_listings(
    source: str, db_engine: Engine | None = None, now: datetime | None = None
) -> int:
    """Move long-dead-looking listings out of the check rotation.

    A listing qualifies only if it's been around a long time AND hasn't shown
    up in any saved search for a long time. Both conditions matter: a
    genuinely popular listing that's been active for a year still turns up in
    search, and a listing that's merely fallen out of the top 200 recently is
    a *candidate for checking*, not a candidate for retirement.

    Retired, not deleted. The row stays, and ingest_saved_search flips it back
    to active for free if it ever reappears.
    """
    db_engine = db_engine or default_engine
    now = now or datetime.now(UTC)

    stale_cutoff = now - timedelta(days=settings.stale_after_days)
    unseen_cutoff = now - timedelta(days=settings.unseen_after_days)

    retired = 0
    with Session(db_engine) as session:
        candidates = session.exec(
            select(Listing).where(
                Listing.source == source,
                Listing.status == ListingStatus.active,
                Listing.first_seen_at < stale_cutoff,
                Listing.last_seen_at < unseen_cutoff,
            )
        ).all()

        for listing in candidates:
            listing.status = ListingStatus.stale
            session.add(listing)
            retired += 1

        session.commit()

    return retired


def check_listings_for_source(
    source: str,
    client: SourceClient | None = None,
    db_engine: Engine | None = None,
    budget: int | None = None,
    now: datetime | None = None,
) -> CheckResult:
    """client/db_engine are injectable so tests can run against a fake client
    and a throwaway DB instead of hitting the real source or Postgres.

    budget overrides the quota-derived cap, which is what tests use to get
    deterministic behavior without a get_rate_limit-capable fake.
    """
    client = client or PULL_BASED_SOURCES[source]()
    db_engine = db_engine or default_engine
    assert_schema_current(db_engine, Listing)
    now = now or datetime.now(UTC)

    result = CheckResult()
    result.retired_stale = retire_stale_listings(source, db_engine=db_engine, now=now)

    if budget is None:
        budget = resolve_budget(client, settings.disappearance_check_budget, settings.quota_reserve)
    if budget <= 0:
        return result

    proven_alive_cutoff = now - timedelta(seconds=settings.proven_alive_seconds)

    with Session(db_engine) as session:
        # Anything a saved search saw since the cutoff is alive by definition,
        # so it never enters the candidate set and costs nothing. Counting the
        # skips separately makes the saving visible in the logs, which is the
        # only way to tell this design is actually paying off.
        result.skipped_proven_alive = len(
            session.exec(
                select(Listing).where(
                    Listing.source == source,
                    Listing.status == ListingStatus.active,
                    Listing.last_seen_at >= proven_alive_cutoff,
                )
            ).all()
        )

        # Ordered by least-recently-*checked*, not least-recently-seen. Every
        # candidate is by definition out of search coverage, so last_seen_at
        # barely varies among them and would keep re-picking the same few.
        # Never-checked listings go first, then the longest since a check,
        # which spreads a fixed budget fairly across however many candidates
        # there are: with N candidates and a budget of B, each listing comes
        # round every ceil(N/B) passes.
        candidates = session.exec(
            select(Listing)
            .where(
                Listing.source == source,
                Listing.status == ListingStatus.active,
                Listing.last_seen_at < proven_alive_cutoff,
            )
            .order_by(
                # Listings awaiting a second strike go first. They're the
                # highest-value calls available: each one either confirms a
                # sale (which is the comp data this all exists for) or clears
                # a false alarm. Without this they'd sort to the back, having
                # just been checked, and confirmation would take a full
                # rotation of the candidate pool.
                col(Listing.missing_since).is_(None).asc(),
                col(Listing.last_checked_at).asc().nullsfirst(),
                col(Listing.last_seen_at).asc(),
            )
            .limit(budget)
        ).all()

        for listing in candidates:
            try:
                found = client.get_item(listing.source_id)
            except QuotaExhaustedError:
                # Stop immediately and keep what's already been committed.
                # Retrying can't help until the daily window resets, and
                # every further attempt burns an allowance that's at zero.
                result.quota_exhausted = True
                break

            listing.last_checked_at = now
            result.checked += 1

            # Enrich first, and unconditionally. The call is already paid for,
            # and an *ending* listing's body is the most valuable one there is:
            # it carries the real itemEndDate and estimatedSoldQuantity that
            # sale scoring wants. Doing this only on the "still alive" branch
            # threw that away for exactly the listings that mattered most.
            if isinstance(found, dict):
                enrich_from_item_body(listing, found)
                result.enriched += 1

                # Re-derive the extracted fields, because enrichment can have
                # just supplied the input they were missing.
                #
                # `aspects` is a getItem-only field, so a listing is ingested
                # and extracted with no aspects at all, and only learns them
                # here, hours or days later. Extraction never revisits it:
                # `extract_all(only_new=True)` keys on `variant_signals IS
                # NULL` and this row was extracted long ago. So the row stayed
                # permanently one enrichment behind, with a capacity that was
                # derivable and simply not derived.
                #
                # Measured 2026-08-10: 74 listings (0.28%), every one of them
                # a console or phone whose capacity lives only in aspects, all
                # already checked, all still reading `capacity_gb = NULL`. The
                # only thing that ever fixed them was a manual full
                # re-extraction, which is not a mechanism.
                #
                # Cheap enough to do unconditionally: extraction is regex over
                # a title plus a dict lookup, against a network round-trip
                # this loop has already paid for.
                variant = extract_variant(
                    listing.title, listing.aspects, listing.category, listing.condition
                )
                listing.lot_size = variant.lot_size
                listing.completeness = variant.completeness
                listing.has_defect = variant.has_defect
                listing.is_accessory = variant.is_accessory
                listing.price_is_from = variant.price_is_from
                listing.capacity_gb = variant.capacity_gb
                listing.spec_generation = variant.spec_generation
                listing.form_factor = variant.form_factor
                listing.model_key = variant.model_key
                listing.variant_signals = variant.signals

            # A 404 is NOT the signal. eBay keeps serving ended listings at
            # HTTP 200 with estimatedAvailabilityStatus OUT_OF_STOCK and/or a
            # past itemEndDate; measured against production, zero of eight
            # vanished listings 404'd. Reading only the status code meant
            # nothing was ever marked sold.
            # See docs/decisions/0011-ebay-does-not-404-ended-listings.md.
            has_ended = found is None or listing_has_ended(found, now=now)

            if has_ended:
                if listing.missing_since is None:
                    # First strike. likely_sold is terminal (the candidate
                    # query only looks at active listings) and the row becomes
                    # comp data, so one transient 404 would mean a permanent
                    # false comp in the dataset this project exists to build.
                    # Costs one extra call per genuine sale, which is cheap
                    # because sales are a small fraction of checks.
                    listing.missing_since = now
                    result.pending_confirmation += 1
                else:
                    listing.status = ListingStatus.likely_sold
                    result.marked_sold += 1
                    # Score it now, not at query time in stage 4: relist
                    # detection depends on what the database looked like around
                    # the disappearance, and the same query run months later
                    # would be looking at a different world.
                    relist = find_relist(listing, db_engine=db_engine, now=now)
                    assessment = score_sale(listing, relist=relist, now=now)
                    listing.sale_confidence = assessment.sale_confidence
                    listing.price_confidence = assessment.price_confidence
                    listing.sale_signals = assessment.signals
                    if relist is not None:
                        result.detected_relists += 1
            else:
                if listing.missing_since is not None:
                    # It came back, so the earlier miss was a blip. Exactly the
                    # case the two-strike rule exists to catch.
                    listing.missing_since = None
                    result.recovered += 1
            # A confirmed-alive listing deliberately does NOT get last_seen_at
            # refreshed, even though stage 2 did exactly that. The two columns
            # now mean different things: last_seen_at is "last seen in search",
            # which is what drives both the proven-alive skip and retirement,
            # while last_checked_at is "last time we spent a call on it".
            # Bumping last_seen_at here would conflate them and make
            # retirement unreachable, because any listing still in the check
            # rotation would have its unseen clock reset every pass and could
            # never age past unseen_after_days. That's precisely backwards:
            # a listing that's alive but permanently out of search coverage is
            # the exact thing retirement exists to stop paying for.

            session.add(listing)

        session.commit()

    return result


def check_ebay_listings(
    client: EbayClient | None = None, db_engine: Engine | None = None
) -> CheckResult:
    """Thin backward-compatible wrapper around check_listings_for_source("ebay", ...)."""
    return check_listings_for_source("ebay", client=client, db_engine=db_engine)


def check_all_sources(db_engine: Engine | None = None) -> CheckResult:
    """Runs check_listings_for_source for every source that is both polled and
    a comp source.

    Deliberately intersects the two sets rather than looping over
    PULL_BASED_SOURCES: disappearance tracking exists solely to infer sold
    prices, so running it on a source whose prices are never used as comps
    would spend scarce API budget producing data nothing reads.
    See docs/decisions/0008-price-oracle-and-valuation-clients.md.
    """
    total = CheckResult()
    for source in PULL_BASED_SOURCES:
        if source not in COMP_SOURCES:
            continue
        total += check_listings_for_source(source, db_engine=db_engine)
    return total


if __name__ == "__main__":
    result = check_all_sources()
    print(
        f"Checked {result.checked}, marked {result.marked_sold} likely_sold "
        f"(of which {result.detected_relists} look like relists, not sales), "
        f"{result.pending_confirmation} awaiting a second strike, "
        f"{result.recovered} recovered after a false alarm, "
        f"skipped {result.skipped_proven_alive} already proven alive, "
        f"retired {result.retired_stale} stale"
        + (" (stopped early: quota exhausted)" if result.quota_exhausted else "")
    )

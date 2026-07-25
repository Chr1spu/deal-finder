from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from api.models import Listing, ListingStatus
from api.settings import settings
from connectors.disappearance_check import (
    COMP_SOURCES,
    EBAY_DAILY_BROWSE_LIMIT,
    check_all_sources,
    check_ebay_listings,
    check_listings_for_source,
    estimate_daily_calls,
    resolve_budget,
    retire_stale_listings,
)
from connectors.ebay import RateLimit


class FakeEbayClient:
    """get_item returns None for IDs in `gone`, a dummy payload otherwise.
    Mirrors the real client's 404-means-sold contract without any network.

    body lets a test supply a realistic getItem response, which carries fields
    itemSummary never does (localizedAspects, estimatedSoldQuantity) and which
    the checker is supposed to harvest for free.
    """

    def __init__(self, gone: set[str], body: dict | None = None):
        self._gone = gone
        self._body = body
        self.calls: list[str] = []

    def get_item(self, item_id: str) -> dict | None:
        self.calls.append(item_id)
        if item_id in self._gone:
            return None
        return {"itemId": item_id, **(self._body or {})}


def _listing(test_engine, source_id: str | None = None) -> Listing:
    """Re-read a listing from a fresh session, so assertions see committed state."""
    with Session(test_engine) as session:
        statement = select(Listing)
        if source_id is not None:
            statement = statement.where(Listing.source_id == source_id)
        return session.exec(statement).one()


def seed_listing(
    session: Session,
    source_id: str,
    source: str = "ebay",
    status: ListingStatus = ListingStatus.active,
    last_seen_days_ago: float = 1.0,
    first_seen_days_ago: float = 1.0,
) -> None:
    now = datetime.now(timezone.utc)
    session.add(
        Listing(
            source=source,
            source_id=source_id,
            title=f"item {source_id}",
            price=10.0,
            url=f"https://ebay.com/{source_id}",
            status=status,
            first_seen_at=now - timedelta(days=first_seen_days_ago),
            last_seen_at=now - timedelta(days=last_seen_days_ago),
        )
    )
    session.commit()


def test_first_disappearance_is_not_enough_to_mark_sold(test_engine):
    """likely_sold is terminal and the row becomes comp data, so a single
    transient 404 would put a permanent false comp into the one dataset this
    project exists to build."""
    with Session(test_engine) as session:
        seed_listing(session, "maybe-sold")

    result = check_ebay_listings(client=FakeEbayClient(gone={"maybe-sold"}), db_engine=test_engine)

    assert (result.marked_sold, result.pending_confirmation) == (0, 1)
    with Session(test_engine) as session:
        listing = session.exec(select(Listing)).one()
        assert listing.status == ListingStatus.active
        assert listing.missing_since is not None


def test_second_consecutive_disappearance_marks_it_sold(test_engine):
    with Session(test_engine) as session:
        seed_listing(session, "sold-1")

    client = FakeEbayClient(gone={"sold-1"})
    check_ebay_listings(client=client, db_engine=test_engine)
    first_strike_at = _listing(test_engine).missing_since

    result = check_ebay_listings(client=client, db_engine=test_engine)

    assert result.marked_sold == 1
    listing = _listing(test_engine)
    assert listing.status == ListingStatus.likely_sold
    assert listing.last_checked_at is not None
    assert listing.missing_since == first_strike_at, (
        "missing_since keeps the *first* miss, which is a closer estimate of "
        "when the item actually left the market than the confirming check is"
    )


def test_a_still_alive_listing_is_enriched_from_the_body_we_already_paid_for(test_engine):
    """The checker was calling getItem and reading only the status code,
    discarding a body that carries structured Brand/Model aspects and real
    sold quantities. Harvesting it costs nothing: the call is already made.
    See docs/decisions/0006-capture-what-ebay-already-sends.md.
    """
    with Session(test_engine) as session:
        seed_listing(session, "still-here")

    client = FakeEbayClient(
        gone=set(),
        body={
            "localizedAspects": [
                {"type": "STRING", "name": "Brand", "value": "EVGA"},
                {"type": "STRING", "name": "Chipset/GPU Model", "value": "RTX 3090"},
            ],
            "estimatedAvailabilities": [{"estimatedSoldQuantity": 4}],
            "epid": "epid-999",
        },
    )
    result = check_ebay_listings(client=client, db_engine=test_engine)

    assert result.enriched == 1
    listing = _listing(test_engine)
    assert listing.aspects == {"Brand": "EVGA", "Chipset/GPU Model": "RTX 3090"}
    assert listing.sold_quantity == 4
    assert listing.epid == "epid-999"


def test_enrichment_does_not_happen_for_a_listing_that_is_gone(test_engine):
    """A 404 has no body to harvest, and must not be mistaken for one."""
    with Session(test_engine) as session:
        seed_listing(session, "gone")

    result = check_ebay_listings(client=FakeEbayClient(gone={"gone"}), db_engine=test_engine)

    assert result.enriched == 0
    assert _listing(test_engine).aspects is None


def test_confirming_a_sale_records_confidence_and_its_reasoning(test_engine):
    """Stage 4 weights comps by this, so it has to be written at confirmation
    time, not recomputed later: relist detection depends on what the database
    looked like around the disappearance."""
    with Session(test_engine) as session:
        seed_listing(session, "sold-with-score")

    client = FakeEbayClient(gone={"sold-with-score"})
    check_ebay_listings(client=client, db_engine=test_engine)
    check_ebay_listings(client=client, db_engine=test_engine)

    listing = _listing(test_engine)
    assert listing.status == ListingStatus.likely_sold
    assert listing.sale_confidence is not None
    assert listing.price_confidence is not None
    assert 0.0 <= listing.sale_confidence <= 1.0
    assert 0.0 <= listing.price_confidence <= 1.0
    assert isinstance(listing.sale_signals, dict)


def test_a_relisted_item_is_confirmed_sold_but_with_low_confidence(test_engine):
    """The item is still on the market under a new id, so it didn't sell. It
    still gets marked (the original listing really is gone), but its price
    must not carry normal weight as a comp."""
    with Session(test_engine) as session:
        seed_listing(session, "original")
        seed_listing(session, "relisted", last_seen_days_ago=0, first_seen_days_ago=0)
        for source_id in ("original", "relisted"):
            row = session.exec(select(Listing).where(Listing.source_id == source_id)).one()
            row.image_hash = "same-photo"
            session.add(row)
        session.commit()

    client = FakeEbayClient(gone={"original"})
    check_ebay_listings(client=client, db_engine=test_engine)
    result = check_ebay_listings(client=client, db_engine=test_engine)

    assert result.detected_relists == 1
    sold = _listing(test_engine, "original")
    assert sold.status == ListingStatus.likely_sold
    assert sold.sale_confidence < 0.2
    assert sold.sale_signals["relisted_as"] == "relisted"


def test_a_listing_that_comes_back_clears_its_strike(test_engine):
    """Exactly the false alarm the two-strike rule exists to absorb."""
    with Session(test_engine) as session:
        seed_listing(session, "blip")

    check_ebay_listings(client=FakeEbayClient(gone={"blip"}), db_engine=test_engine)
    assert _listing(test_engine).missing_since is not None

    result = check_ebay_listings(client=FakeEbayClient(gone=set()), db_engine=test_engine)

    assert (result.recovered, result.marked_sold) == (1, 0)
    listing = _listing(test_engine)
    assert listing.missing_since is None
    assert listing.status == ListingStatus.active


def test_reappearing_in_a_search_also_clears_a_strike(test_engine):
    """A search hit is free proof of life, so it should clear a pending
    strike without spending a confirmation call on it."""
    with Session(test_engine) as session:
        seed_listing(session, "back-in-search")
    check_ebay_listings(client=FakeEbayClient(gone={"back-in-search"}), db_engine=test_engine)
    assert _listing(test_engine).missing_since is not None

    with Session(test_engine) as session:
        listing = session.exec(select(Listing)).one()
        listing.missing_since = None  # what ingest_saved_search does on a hit
        session.add(listing)
        session.commit()

    assert _listing(test_engine).missing_since is None


def test_pending_confirmations_are_checked_before_anything_else(test_engine):
    """A listing awaiting its second strike is the highest-value call
    available: it either confirms a sale or clears a false alarm. Left in
    normal order it would sort to the back, having just been checked."""
    with Session(test_engine) as session:
        seed_listing(session, "never-checked", last_seen_days_ago=9)
        seed_listing(session, "awaiting-strike-two", last_seen_days_ago=2)

    with Session(test_engine) as session:
        listing = session.exec(
            select(Listing).where(Listing.source_id == "awaiting-strike-two")
        ).one()
        listing.missing_since = datetime.now(timezone.utc) - timedelta(hours=6)
        listing.last_checked_at = datetime.now(timezone.utc) - timedelta(hours=6)
        session.add(listing)
        session.commit()

    client = FakeEbayClient(gone=set())
    check_listings_for_source("ebay", client=client, db_engine=test_engine, budget=1)

    assert client.calls == ["awaiting-strike-two"]


def test_still_active_listing_keeps_status_and_records_the_check(test_engine):
    """A confirmed-alive listing gets last_checked_at stamped, and its
    last_seen_at deliberately left alone. The two columns mean different
    things now: seen-in-search versus paid-for-a-call. See the next test for
    why conflating them breaks retirement."""
    with Session(test_engine) as session:
        seed_listing(session, "still-here")
        original_last_seen = session.exec(select(Listing)).one().last_seen_at

    result = check_ebay_listings(client=FakeEbayClient(gone=set()), db_engine=test_engine)

    assert (result.checked, result.marked_sold) == (1, 0)
    with Session(test_engine) as session:
        listing = session.exec(select(Listing).where(Listing.source_id == "still-here")).one()
        assert listing.status == ListingStatus.active
        assert listing.last_checked_at is not None
        assert listing.last_seen_at == original_last_seen, (
            "a check must not count as a search sighting, or the listing's "
            "unseen clock resets every pass and it can never retire"
        )


def test_a_listing_stuck_in_the_check_rotation_can_still_retire(test_engine):
    """The regression that motivated splitting the two timestamps.

    A listing that's alive but permanently below eBay's 200-result cap never
    reappears in search, so it stays a candidate and costs a call every pass,
    forever. Retirement exists to stop paying for exactly that. If a
    successful check refreshed last_seen_at, this listing's unseen clock would
    reset on every pass and retirement could never fire, which is precisely
    backwards.
    """
    with Session(test_engine) as session:
        seed_listing(session, "alive-but-invisible", first_seen_days_ago=200, last_seen_days_ago=100)

    client = FakeEbayClient(gone=set())
    check_listings_for_source("ebay", client=client, db_engine=test_engine)

    with Session(test_engine) as session:
        listing = session.exec(select(Listing)).one()
        assert listing.status == ListingStatus.stale, (
            "an old, long-unseen, still-alive listing must leave the rotation"
        )


def test_budget_is_spread_across_candidates_by_least_recently_checked(test_engine):
    """Every candidate is out of search coverage, so last_seen_at barely
    varies between them. Ordering on last_checked_at is what stops a small
    budget re-checking the same few listings while others never come round."""
    now = datetime.now(timezone.utc)
    with Session(test_engine) as session:
        seed_listing(session, "checked-recently", last_seen_days_ago=5)
        seed_listing(session, "checked-long-ago", last_seen_days_ago=5)
        seed_listing(session, "never-checked", last_seen_days_ago=5)

        for source_id, checked_days_ago in [("checked-recently", 1), ("checked-long-ago", 9)]:
            row = session.exec(select(Listing).where(Listing.source_id == source_id)).one()
            row.last_checked_at = now - timedelta(days=checked_days_ago)
            session.add(row)
        session.commit()

    client = FakeEbayClient(gone=set())
    check_listings_for_source("ebay", client=client, db_engine=test_engine, budget=2)

    assert client.calls == ["never-checked", "checked-long-ago"]


def test_only_checks_active_listings_not_already_sold_ones(test_engine):
    with Session(test_engine) as session:
        seed_listing(session, "already-sold", status=ListingStatus.likely_sold)

    result = check_ebay_listings(client=FakeEbayClient(gone=set()), db_engine=test_engine)

    assert (result.checked, result.marked_sold) == (0, 0)


def test_recently_seen_listing_is_skipped_without_an_api_call(test_engine):
    """The core stage 2.5 saving: eBay only returns *active* listings from
    search, so a listing ingestion just saw is provably alive and must never
    cost a get_item call. This is what makes the budget scale with churn
    rather than with corpus size."""
    with Session(test_engine) as session:
        seed_listing(session, "seen-just-now", last_seen_days_ago=0)
        seed_listing(session, "not-seen-in-ages", last_seen_days_ago=5)

    client = FakeEbayClient(gone=set())
    result = check_listings_for_source("ebay", client=client, db_engine=test_engine)

    assert client.calls == ["not-seen-in-ages"], "only the doubtful listing costs a call"
    assert result.checked == 1
    assert result.skipped_proven_alive == 1


def test_candidates_are_checked_oldest_unseen_first(test_engine):
    """Budget is scarce, so it goes to the listings likeliest to actually be
    gone, not to whatever the database happens to return first."""
    with Session(test_engine) as session:
        seed_listing(session, "seen-2-days-ago", last_seen_days_ago=2)
        seed_listing(session, "seen-9-days-ago", last_seen_days_ago=9)
        seed_listing(session, "seen-5-days-ago", last_seen_days_ago=5)

    client = FakeEbayClient(gone=set())
    check_listings_for_source("ebay", client=client, db_engine=test_engine, budget=2)

    assert client.calls == ["seen-9-days-ago", "seen-5-days-ago"]


def test_budget_caps_how_many_listings_get_checked(test_engine):
    with Session(test_engine) as session:
        for i in range(10):
            seed_listing(session, f"listing-{i}", last_seen_days_ago=i + 1)

    client = FakeEbayClient(gone=set())
    result = check_listings_for_source("ebay", client=client, db_engine=test_engine, budget=3)

    assert result.checked == 3
    assert len(client.calls) == 3


def test_zero_budget_spends_nothing(test_engine):
    with Session(test_engine) as session:
        seed_listing(session, "would-be-checked", last_seen_days_ago=5)

    client = FakeEbayClient(gone=set())
    result = check_listings_for_source("ebay", client=client, db_engine=test_engine, budget=0)

    assert client.calls == []
    assert result.checked == 0


def test_check_all_sources_loops_every_registered_source(test_engine, monkeypatch):
    """PULL_BASED_SOURCES only has "ebay" registered today (Depop doesn't
    exist yet), so fake out a second source to prove the loop itself works
    per-source rather than being hardcoded to eBay."""

    class FakeGoneClient:
        def get_item(self, item_id: str) -> dict | None:
            return None

    class FakeStillActiveClient:
        def get_item(self, item_id: str) -> dict | None:
            return {"id": item_id}

    monkeypatch.setattr(
        "connectors.disappearance_check.PULL_BASED_SOURCES",
        {"ebay": FakeGoneClient, "depop": FakeStillActiveClient},
    )
    # Both are comp sources here, since this test is about the loop shape.
    # The separate test below covers a polled source that is NOT a comp source.
    monkeypatch.setattr(
        "connectors.disappearance_check.COMP_SOURCES", frozenset({"ebay", "depop"})
    )

    with Session(test_engine) as session:
        seed_listing(session, "ebay-1", source="ebay")
        seed_listing(session, "depop-1", source="depop")

    # Two passes, because marking sold now needs two consecutive misses.
    check_all_sources(db_engine=test_engine)
    result = check_all_sources(db_engine=test_engine)

    assert (result.checked, result.marked_sold) == (2, 1)
    with Session(test_engine) as session:
        ebay_listing = session.exec(select(Listing).where(Listing.source_id == "ebay-1")).one()
        depop_listing = session.exec(select(Listing).where(Listing.source_id == "depop-1")).one()
        assert ebay_listing.status == ListingStatus.likely_sold, "the source whose client 404s"
        assert depop_listing.status == ListingStatus.active, "the source whose client finds it"


# --- zombie retirement ---------------------------------------------------


def test_old_and_long_unseen_listing_is_retired(test_engine):
    """eBay fixed-price listings can auto-renew forever. Without retirement
    the candidate set only ever grows, which is the one genuinely unbounded
    thing in this design."""
    with Session(test_engine) as session:
        seed_listing(session, "zombie", first_seen_days_ago=200, last_seen_days_ago=100)

    retired = retire_stale_listings("ebay", db_engine=test_engine)

    assert retired == 1
    with Session(test_engine) as session:
        assert session.exec(select(Listing)).one().status == ListingStatus.stale


def test_old_but_still_appearing_listing_is_not_retired(test_engine):
    """Age alone isn't evidence of anything. A listing that's been up for a
    year but still shows in search every day is simply a popular live listing."""
    with Session(test_engine) as session:
        seed_listing(session, "old-but-alive", first_seen_days_ago=200, last_seen_days_ago=0)

    assert retire_stale_listings("ebay", db_engine=test_engine) == 0
    with Session(test_engine) as session:
        assert session.exec(select(Listing)).one().status == ListingStatus.active


def test_recently_created_but_unseen_listing_is_not_retired(test_engine):
    """A young listing that's dropped out of the top 200 is a candidate for
    *checking*, not for retirement. Retiring it would skip the very check
    that would have caught the sale."""
    with Session(test_engine) as session:
        seed_listing(session, "young-and-quiet", first_seen_days_ago=2, last_seen_days_ago=1.5)

    assert retire_stale_listings("ebay", db_engine=test_engine) == 0
    with Session(test_engine) as session:
        assert session.exec(select(Listing)).one().status == ListingStatus.active


def test_retired_listings_leave_the_check_rotation(test_engine):
    with Session(test_engine) as session:
        seed_listing(session, "zombie", first_seen_days_ago=200, last_seen_days_ago=100)

    client = FakeEbayClient(gone=set())
    result = check_listings_for_source("ebay", client=client, db_engine=test_engine)

    assert result.retired_stale == 1
    assert client.calls == [], "a retired listing costs no API call in the same pass"


# --- budget resolution ---------------------------------------------------


class FakeQuotaClient:
    def __init__(self, remaining: int | None):
        self._remaining = remaining

    def get_item(self, item_id: str) -> dict | None:
        return {"itemId": item_id}

    def get_rate_limit(self, resource: str = "buy.browse") -> RateLimit | None:
        if self._remaining is None:
            return None
        return RateLimit(limit=5000, remaining=self._remaining, reset=None)


def test_budget_is_capped_by_real_remaining_quota_minus_reserve():
    """Ingestion must never be starved by the checker: it's both the product
    and the cheapest liveness signal available (200 listings per call)."""
    budget = resolve_budget(FakeQuotaClient(remaining=2000), configured=800, reserve=1500)
    assert budget == 500, "2000 remaining minus a 1500 reserve leaves 500, under the 800 configured"


def test_budget_never_exceeds_the_configured_cap():
    budget = resolve_budget(FakeQuotaClient(remaining=5000), configured=800, reserve=1500)
    assert budget == 800


def test_budget_is_zero_when_the_reserve_is_already_gone():
    budget = resolve_budget(FakeQuotaClient(remaining=100), configured=800, reserve=1500)
    assert budget == 0, "never negative, and never spend into the reserve"


def test_budget_falls_back_to_configured_when_quota_is_unknown():
    """A client that can't report quota (a test fake, a future Depop client)
    must still be checkable rather than silently doing nothing."""
    assert resolve_budget(FakeQuotaClient(remaining=None), configured=800, reserve=1500) == 800
    assert resolve_budget(FakeEbayClient(gone=set()), configured=800, reserve=1500) == 800


# --- configuration sanity ------------------------------------------------

CURRENT_SAVED_SEARCH_COUNT = 64


def test_default_settings_fit_inside_the_daily_quota():
    """The outage this whole design exists to prevent was an arithmetic
    failure nobody had written down: one get_item per active listing, four
    times a day, wanted ~42,000 calls against an allowance of 5,000.

    If this fails, either the intervals got shorter, the budget got bigger,
    or a lot of saved searches were added. Fix the configuration rather than
    the assertion, otherwise the pipeline goes down again at 3am.
    """
    ingest_calls, check_calls = estimate_daily_calls(CURRENT_SAVED_SEARCH_COUNT)

    assert ingest_calls + check_calls < EBAY_DAILY_BROWSE_LIMIT, (
        f"configured spend is {ingest_calls} ingest + {check_calls} check = "
        f"{ingest_calls + check_calls}, over eBay's {EBAY_DAILY_BROWSE_LIMIT}/day"
    )


def test_proven_alive_window_outlasts_the_ingest_interval():
    """The whole budget design rests on most listings being refreshed by
    ingestion before their proven-alive window expires.

    If proven_alive_seconds drops below the ingest interval, every listing
    goes stale between runs, the entire corpus becomes check candidates every
    single pass, and the budget is instantly meaningless. That failure is
    silent: nothing errors, the checker just starts burning its whole
    allowance re-confirming listings that ingestion already proved alive.

    The 1.5x margin is for ingest runtime, since a run takes minutes and a
    listing seen at the start of one shouldn't expire before the next begins.
    """
    assert settings.proven_alive_seconds >= settings.ingest_interval_seconds * 1.5, (
        f"proven_alive_seconds ({settings.proven_alive_seconds}) must comfortably exceed "
        f"ingest_interval_seconds ({settings.ingest_interval_seconds}); otherwise every "
        f"listing becomes a check candidate on every pass"
    )


def test_quota_reserve_covers_a_full_day_of_ingest():
    """Ingest must never be the thing that gets starved: it's the product, and
    it's the cheapest liveness signal there is (200 listings per call). The
    reserve has to be big enough that checking stops first."""
    ingest_calls, _ = estimate_daily_calls(CURRENT_SAVED_SEARCH_COUNT)

    assert settings.quota_reserve >= ingest_calls, (
        f"reserve {settings.quota_reserve} is below a full day of ingest "
        f"({ingest_calls}), so the checker could starve ingestion"
    )


# --- comp sources versus polled sources -----------------------------------
# See docs/decisions/0008-price-oracle-and-valuation-clients.md. eBay is the
# only source whose prices are trustworthy enough to value others against.
# Depop gets polled so its listings can be *scored*, never to build history.


def test_a_polled_source_that_is_not_a_comp_source_is_never_checked(test_engine, monkeypatch):
    """The whole point of the COMP_SOURCES split. Disappearance tracking exists
    only to infer sold prices, so running it on a source whose prices are never
    used as comps would spend scarce API budget producing data nothing reads.

    If this fails, someone has added a connector to PULL_BASED_SOURCES and
    silently enrolled it in comp building. That is the mistake the two sets
    exist to prevent.
    """
    calls: list[str] = []

    class RecordingClient:
        def get_item(self, item_id: str) -> dict | None:
            calls.append(item_id)
            return None

    monkeypatch.setattr(
        "connectors.disappearance_check.PULL_BASED_SOURCES",
        {"ebay": RecordingClient, "depop": RecordingClient},
    )
    monkeypatch.setattr("connectors.disappearance_check.COMP_SOURCES", frozenset({"ebay"}))

    with Session(test_engine) as session:
        seed_listing(session, "ebay-1", source="ebay")
        seed_listing(session, "depop-1", source="depop")

    check_all_sources(db_engine=test_engine)

    assert calls == ["ebay-1"], "the polled non-comp source must cost zero API calls"
    with Session(test_engine) as session:
        depop = session.exec(select(Listing).where(Listing.source_id == "depop-1")).one()
        assert depop.status == ListingStatus.active
        assert depop.last_checked_at is None, "and must not even be recorded as checked"


def test_ebay_is_the_only_comp_source():
    """A guard on the project's central architectural claim rather than on any
    one function. If a source is added here, sold-price inference starts
    running against it, so it should be a deliberate ADR-level decision."""
    assert set(COMP_SOURCES) == {"ebay"}


def test_quota_reserve_leaves_room_for_a_full_day_of_checking():
    """The reserve protects ingest from the checker, and it can silently do
    too good a job.

    Observed live on 2026-07-25: reserve was 2,000, remaining hit exactly
    2,000, and resolve_budget returned 0. Sold-detection halted for 15 hours
    with ~1,550 calls left to expire unused, and nothing logged a problem
    because a budget of zero is indistinguishable from "nothing to check".

    Two bounds, both of which have to hold:
      above  a full day of ingest, or the checker really can starve it
      below  what the planned check volume needs, or checking halts early
    """
    from connectors.disappearance_check import EBAY_DAILY_BROWSE_LIMIT

    ingest_calls, check_calls = estimate_daily_calls(64)

    assert settings.quota_reserve > ingest_calls, (
        f"reserve ({settings.quota_reserve}) must exceed a full day of ingest "
        f"({ingest_calls}) or the checker can starve ingestion"
    )
    assert settings.quota_reserve <= EBAY_DAILY_BROWSE_LIMIT - ingest_calls - check_calls, (
        f"reserve ({settings.quota_reserve}) leaves only "
        f"{EBAY_DAILY_BROWSE_LIMIT - settings.quota_reserve - ingest_calls} calls for checking, "
        f"but the configured schedule wants {check_calls}. The checker will halt partway "
        f"through the day and accumulate no comps after that, silently."
    )


def test_the_checker_still_has_budget_at_the_reserve_boundary():
    """A direct regression on the halt: at remaining == reserve the old
    configuration returned exactly 0."""
    from connectors.ebay import RateLimit

    class AtBoundary:
        def get_rate_limit(self):
            return RateLimit(EBAY_DAILY_BROWSE_LIMIT, settings.quota_reserve + 700, None)

    assert resolve_budget(AtBoundary(), settings.disappearance_check_budget,
                          settings.quota_reserve) > 0

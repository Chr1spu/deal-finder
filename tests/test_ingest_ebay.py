import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlmodel import Session, select

from api.models import Listing, ListingStatus, PriceObservation, SavedSearch
from connectors.ebay import SearchResult
from connectors.ingest_ebay import ingest_all, ingest_saved_search
from systems.ratelimit import QuotaExhaustedError

FIXTURE = Path(__file__).parent / "fixtures" / "ebay_item_summary.json"


class FakeEbayClient:
    """Stands in for connectors.ebay.EbayClient. No network, no OAuth."""

    def __init__(self, items: list[dict], total: int | None = None):
        self._items = items
        # total defaults to "eBay had exactly what it returned", so tests that
        # don't care about truncation don't have to think about it.
        self._total = len(items) if total is None else total

    def search_items(self, query: str, limit: int = 50) -> SearchResult:
        return SearchResult(items=self._items, total=self._total)


def fake_image_hasher(url: str) -> str:
    """Stands in for connectors.image_hash.fetch_and_hash. No network."""
    return f"hash-of-{url}"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def seed_saved_search(test_engine, keyword: str = "nintendo switch") -> SavedSearch:
    with Session(test_engine) as session:
        saved_search = SavedSearch(keyword=keyword)
        session.add(saved_search)
        session.commit()
        session.refresh(saved_search)
        return saved_search


def test_ingest_saved_search_inserts_new_listing(test_engine):
    saved_search = seed_saved_search(test_engine)
    client = FakeEbayClient([load_fixture()])

    result = ingest_saved_search(
        saved_search, client=client, db_engine=test_engine, image_hasher=fake_image_hasher
    )

    assert (result.inserted, result.updated) == (1, 0)
    with Session(test_engine) as session:
        rows = session.exec(select(Listing)).all()
        assert len(rows) == 1
        assert rows[0].source_id == "v1|123456789012|0"
        assert rows[0].price == 249.99
        assert rows[0].image_hash == f"hash-of-{rows[0].images[0]}"


def test_first_ingest_records_an_opening_price_observation(test_engine):
    """Without a row at insert time, a listing's price chart would begin at
    whenever the price first happened to change, which reads as though nothing
    was known before that."""
    saved_search = seed_saved_search(test_engine)
    ingest_saved_search(
        saved_search,
        client=FakeEbayClient([load_fixture()]),
        db_engine=test_engine,
        image_hasher=fake_image_hasher,
    )

    with Session(test_engine) as session:
        observations = session.exec(select(PriceObservation)).all()
        assert len(observations) == 1
        assert observations[0].price == 249.99
        assert observations[0].shipping_cost == 12.50


def test_price_change_is_recorded_and_the_old_price_is_not_lost(test_engine):
    """The regression that motivated the whole table: ingestion used to
    overwrite price in place, so a drop (the strongest deal signal there is)
    left no trace at all."""
    saved_search = seed_saved_search(test_engine)
    ingest_saved_search(
        saved_search,
        client=FakeEbayClient([load_fixture()]),
        db_engine=test_engine,
        image_hasher=fake_image_hasher,
    )

    dropped = load_fixture()
    dropped["price"]["value"] = "199.99"
    result = ingest_saved_search(
        saved_search,
        client=FakeEbayClient([dropped]),
        db_engine=test_engine,
        image_hasher=fake_image_hasher,
    )

    assert result.price_changes == 1
    with Session(test_engine) as session:
        prices = [o.price for o in session.exec(select(PriceObservation).order_by(PriceObservation.id)).all()]
        assert prices == [249.99, 199.99], "both the old and new price survive"
        assert session.exec(select(Listing)).one().price == 199.99


def test_unchanged_price_records_nothing_new(test_engine):
    """Ingest runs every couple of hours against ~10k listings. Recording an
    observation regardless of change would add hundreds of thousands of rows a
    day that all say 'nothing happened'."""
    saved_search = seed_saved_search(test_engine)
    client = FakeEbayClient([load_fixture()])
    for _ in range(3):
        ingest_saved_search(
            saved_search, client=client, db_engine=test_engine, image_hasher=fake_image_hasher
        )

    with Session(test_engine) as session:
        assert len(session.exec(select(PriceObservation)).all()) == 1


def test_shipping_and_buying_options_are_captured(test_engine):
    saved_search = seed_saved_search(test_engine)
    ingest_saved_search(
        saved_search,
        client=FakeEbayClient([load_fixture()]),
        db_engine=test_engine,
        image_hasher=fake_image_hasher,
    )

    with Session(test_engine) as session:
        listing = session.exec(select(Listing)).one()
        assert listing.shipping_cost == 12.50, "cheapest of the offered options, not the first"
        assert listing.is_auction is False
        assert listing.accepts_best_offer is True


def test_saved_search_records_its_result_total(test_engine):
    """eBay reports how many results exist; ingestion only ever sees 200.
    Recording the total makes truncation measurable instead of assumed."""
    saved_search = seed_saved_search(test_engine)
    result = ingest_saved_search(
        saved_search,
        client=FakeEbayClient([load_fixture()], total=4321),
        db_engine=test_engine,
        image_hasher=fake_image_hasher,
    )

    assert result.truncated_searches == 1
    with Session(test_engine) as session:
        tracked = session.exec(select(SavedSearch)).one()
        assert tracked.last_result_total == 4321
        assert tracked.last_run_at is not None


def test_ingest_does_not_rehash_a_listing_that_already_has_a_hash(test_engine):
    """The hasher shouldn't be called again on re-ingest once a listing
    already has an image_hash, only to fill it in when missing."""
    saved_search = seed_saved_search(test_engine)
    client = FakeEbayClient([load_fixture()])
    hash_calls = []

    def counting_hasher(url: str) -> str:
        hash_calls.append(url)
        return f"hash-of-{url}"

    ingest_saved_search(saved_search, client=client, db_engine=test_engine, image_hasher=counting_hasher)
    assert len(hash_calls) == 1

    ingest_saved_search(saved_search, client=client, db_engine=test_engine, image_hasher=counting_hasher)
    assert len(hash_calls) == 1, "already-hashed listing must not be re-hashed on a second ingest run"


def test_ingest_saved_search_upserts_instead_of_duplicating(test_engine):
    saved_search = seed_saved_search(test_engine)
    client = FakeEbayClient([load_fixture()])
    ingest_saved_search(saved_search, client=client, db_engine=test_engine, image_hasher=fake_image_hasher)

    changed = load_fixture()
    changed["price"]["value"] = "199.99"
    client_second_pass = FakeEbayClient([changed])
    ingest_saved_search(
        saved_search, client=client_second_pass, db_engine=test_engine, image_hasher=fake_image_hasher
    )

    with Session(test_engine) as session:
        rows = session.exec(select(Listing)).all()
        assert len(rows) == 1, "same source_id must update the existing row, not insert a second one"
        assert rows[0].price == 199.99


def test_ingest_all_runs_every_saved_search(test_engine):
    seed_saved_search(test_engine, keyword="nintendo switch")
    seed_saved_search(test_engine, keyword="gameboy advance")
    client = FakeEbayClient([load_fixture()])

    total = ingest_all(client=client, db_engine=test_engine, image_hasher=fake_image_hasher)

    assert total.upserted == 2, "one upsert per saved search, even though both hit the same listing"
    assert (total.inserted, total.updated) == (1, 1), (
        "the second search finds the listing the first one just inserted, and that "
        "insert-vs-update split is the arrival-rate signal stage 2.5 added"
    )
    assert total.searches_run == 2
    with Session(test_engine) as session:
        rows = session.exec(select(Listing)).all()
        assert len(rows) == 1, "both searches returning the same source_id still upserts a single row"


def test_one_failing_search_does_not_kill_the_others(test_engine):
    """Before stage 2.5 a single 429 on the first of 64 searches aborted the
    whole run, so one transient error meant zero ingested listings."""
    seed_saved_search(test_engine, keyword="explodes")
    seed_saved_search(test_engine, keyword="works fine")

    class FlakyClient:
        def search_items(self, query: str, limit: int = 50) -> SearchResult:
            if query == "explodes":
                raise httpx.ConnectError("boom")
            return SearchResult(items=[load_fixture()], total=1)

    total = ingest_all(client=FlakyClient(), db_engine=test_engine, image_hasher=fake_image_hasher)

    assert total.searches_failed == 1
    assert total.searches_run == 1
    assert total.inserted == 1, "the healthy search still landed its listing"


def test_quota_exhaustion_stops_the_run_instead_of_retrying_every_search(test_engine):
    """A daily-quota 429 will fail identically for every remaining search, and
    each attempt spends allowance that's already gone. Stop, keep what landed."""
    for i in range(5):
        seed_saved_search(test_engine, keyword=f"search-{i}")

    class QuotaClient:
        def __init__(self):
            self.calls = 0

        def search_items(self, query: str, limit: int = 50) -> SearchResult:
            self.calls += 1
            if self.calls > 2:
                raise QuotaExhaustedError("out of quota")
            return SearchResult(items=[load_fixture()], total=1)

    client = QuotaClient()
    total = ingest_all(client=client, db_engine=test_engine, image_hasher=fake_image_hasher)

    assert total.quota_exhausted is True
    assert client.calls == 3, "stops on the first quota error, doesn't try the remaining searches"
    assert total.searches_run == 2, "keeps what the successful searches landed"
    assert total.searches_failed == 0, "quota exhaustion is a clean stop, not a per-search failure"


def test_reappearing_stale_listing_is_reactivated(test_engine):
    """A stale listing was retired to save API calls, not because it sold.
    Turning up in a search is free proof it's alive, so it comes back."""
    saved_search = seed_saved_search(test_engine)
    client = FakeEbayClient([load_fixture()])
    ingest_saved_search(saved_search, client=client, db_engine=test_engine, image_hasher=fake_image_hasher)

    with Session(test_engine) as session:
        listing = session.exec(select(Listing)).one()
        listing.status = ListingStatus.stale
        session.add(listing)
        session.commit()

    result = ingest_saved_search(
        saved_search, client=client, db_engine=test_engine, image_hasher=fake_image_hasher
    )

    assert result.reactivated == 1
    with Session(test_engine) as session:
        assert session.exec(select(Listing)).one().status == ListingStatus.active


def test_likely_sold_listing_is_not_resurrected_by_a_search_hit(test_engine):
    """Unlike stale, likely_sold is a confirmed outcome backed by a real API
    call, and it's the comp data the whole project is built to collect.
    A search hit must not silently undo it."""
    saved_search = seed_saved_search(test_engine)
    client = FakeEbayClient([load_fixture()])
    ingest_saved_search(saved_search, client=client, db_engine=test_engine, image_hasher=fake_image_hasher)

    with Session(test_engine) as session:
        listing = session.exec(select(Listing)).one()
        listing.status = ListingStatus.likely_sold
        session.add(listing)
        session.commit()

    result = ingest_saved_search(
        saved_search, client=client, db_engine=test_engine, image_hasher=fake_image_hasher
    )

    assert result.reactivated == 0
    with Session(test_engine) as session:
        assert session.exec(select(Listing)).one().status == ListingStatus.likely_sold


def seed_stale_schema_listing(test_engine, **overrides) -> int:
    """A listing as the pre-stage-2.5 ingest wrote them: stage-1 fields only,
    everything from migration 0005 on left at its column default.

    This is the real production shape, not a hypothetical. The live corpus of
    10,496 rows was written by a resident worker running code from before
    those columns existed, so every one of them looks like this.
    """
    fields = {
        "source": "ebay",
        "source_id": "v1|123456789012|0",
        "title": "Nintendo Switch OLED Console - White",
        "price": 249.99,
        "url": "https://www.ebay.com/itm/123456789012",
        "images": ["https://i.ebayimg.com/images/g/main.jpg"],
        "image_hash": "hash-of-old",
    }
    fields.update(overrides)
    with Session(test_engine) as session:
        listing = Listing(**fields)
        session.add(listing)
        session.commit()
        session.refresh(listing)
        assert listing.id is not None
        return listing.id


def test_existing_listing_backfills_fields_added_after_it_was_inserted(test_engine):
    """The bug this guards: the update path refreshed six fields while the
    model grew to thirty-four, so a listing already in the table could never
    acquire a column added later. Every ingest since has *seen* these values
    and thrown them away."""
    saved_search = seed_saved_search(test_engine)
    listing_id = seed_stale_schema_listing(test_engine)

    result = ingest_saved_search(
        saved_search,
        client=FakeEbayClient([load_fixture()]),
        db_engine=test_engine,
        image_hasher=fake_image_hasher,
    )

    assert (result.inserted, result.updated) == (0, 1), "same listing, not a new one"
    with Session(test_engine) as session:
        listing = session.get(Listing, listing_id)
        assert listing is not None
        assert listing.epid == "240012345"
        assert listing.shipping_cost == 12.50
        assert listing.accepts_best_offer is True
        assert listing.seller_feedback_score == 4821
        assert listing.seller_feedback_percent == 99.4
        assert listing.qualified_programs == ["EBAY_PLUS"]
        # Stays None, and that is correct: search responses never carry
        # itemEndDate, so ingest has no way to learn it. Only the
        # disappearance check's full item body can. See ADR 0011.
        assert listing.item_end_date is None


def test_refresh_does_not_blank_a_field_missing_from_a_later_response(test_engine):
    """Additive, like normalizer.enrich_from_item_body: one response that
    omits a field must not erase what an earlier one taught us."""
    saved_search = seed_saved_search(test_engine)
    listing_id = seed_stale_schema_listing(
        test_engine, epid="240012345", seller_feedback_score=4821, shipping_cost=12.50
    )

    sparse = load_fixture()
    for field in ("epid", "seller", "shippingOptions", "qualifiedPrograms"):
        sparse.pop(field)

    ingest_saved_search(
        saved_search,
        client=FakeEbayClient([sparse]),
        db_engine=test_engine,
        image_hasher=fake_image_hasher,
    )

    with Session(test_engine) as session:
        listing = session.get(Listing, listing_id)
        assert listing is not None
        assert listing.epid == "240012345"
        assert listing.seller_feedback_score == 4821
        assert listing.shipping_cost == 12.50


def test_missing_shipping_does_not_manufacture_a_price_observation(test_engine):
    """Regression on the interaction between the two rules above: because a
    missing shipping cost is declined rather than written, comparing the
    *incoming* values against the stored ones would disagree forever and log
    a fresh price change on every single run."""
    saved_search = seed_saved_search(test_engine)
    seed_stale_schema_listing(test_engine, shipping_cost=12.50)

    sparse = load_fixture()
    sparse.pop("shippingOptions")

    for _ in range(3):
        result = ingest_saved_search(
            saved_search,
            client=FakeEbayClient([sparse]),
            db_engine=test_engine,
            image_hasher=fake_image_hasher,
        )
        assert result.price_changes == 0

    with Session(test_engine) as session:
        assert len(session.exec(select(PriceObservation)).all()) == 0


def test_ingest_never_claims_to_know_gtc(test_engine):
    """Ingest sees only search responses, which never carry itemEndDate, so it
    has no basis for a GTC judgement in either direction. Inferring one here is
    what produced a bogus 98.9% GTC rate across the whole corpus.
    See docs/decisions/0011-ebay-does-not-404-ended-listings.md."""
    saved_search = seed_saved_search(test_engine)
    listing_id = seed_stale_schema_listing(test_engine)

    ingest_saved_search(
        saved_search,
        client=FakeEbayClient([load_fixture()]),
        db_engine=test_engine,
        image_hasher=fake_image_hasher,
    )

    with Session(test_engine) as session:
        listing = session.get(Listing, listing_id)
        assert listing is not None
        assert listing.is_gtc is None
        assert listing.item_end_date is None


def test_ingest_does_not_erase_what_the_check_learned_from_a_full_body(test_engine):
    """The disappearance check populates item_end_date and is_gtc from a real
    item body. A later ingest, whose response simply lacks those fields, must
    not blank them: absence in a search response is evidence of nothing."""
    saved_search = seed_saved_search(test_engine)
    listing_id = seed_stale_schema_listing(
        test_engine,
        item_end_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        is_gtc=False,
    )

    ingest_saved_search(
        saved_search,
        client=FakeEbayClient([load_fixture()]),
        db_engine=test_engine,
        image_hasher=fake_image_hasher,
    )

    with Session(test_engine) as session:
        listing = session.get(Listing, listing_id)
        assert listing is not None
        assert listing.item_end_date is not None, "learned from a real body, not discardable"
        assert listing.is_gtc is False

import json
from pathlib import Path

from sqlmodel import Session, select

from api.models import Listing, SavedSearch
from connectors.ingest_ebay import ingest_all, ingest_saved_search

FIXTURE = Path(__file__).parent / "fixtures" / "ebay_item_summary.json"


class FakeEbayClient:
    """Stands in for connectors.ebay.EbayClient. No network, no OAuth."""

    def __init__(self, items: list[dict]):
        self._items = items

    def search_items(self, query: str, limit: int = 50) -> list[dict]:
        return self._items


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

    count = ingest_saved_search(
        saved_search, client=client, db_engine=test_engine, image_hasher=fake_image_hasher
    )

    assert count == 1
    with Session(test_engine) as session:
        rows = session.exec(select(Listing)).all()
        assert len(rows) == 1
        assert rows[0].source_id == "v1|123456789012|0"
        assert rows[0].price == 249.99
        assert rows[0].image_hash == f"hash-of-{rows[0].images[0]}"


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

    assert total == 2, "one upsert per saved search, even though both happen to hit the same listing"
    with Session(test_engine) as session:
        rows = session.exec(select(Listing)).all()
        assert len(rows) == 1, "both searches returning the same source_id still upserts a single row"

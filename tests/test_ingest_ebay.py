import json
from pathlib import Path

from sqlmodel import Session, select

from api.models import Listing
from connectors.ingest_ebay import ingest

FIXTURE = Path(__file__).parent / "fixtures" / "ebay_item_summary.json"


class FakeEbayClient:
    """Stands in for connectors.ebay.EbayClient. No network, no OAuth."""

    def __init__(self, items: list[dict]):
        self._items = items

    def search_items(self, query: str, limit: int = 50) -> list[dict]:
        return self._items


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_ingest_inserts_new_listing(test_engine):
    client = FakeEbayClient([load_fixture()])

    count = ingest(client=client, db_engine=test_engine)

    assert count == 1
    with Session(test_engine) as session:
        rows = session.exec(select(Listing)).all()
        assert len(rows) == 1
        assert rows[0].source_id == "v1|123456789012|0"
        assert rows[0].price == 249.99


def test_ingest_upserts_instead_of_duplicating(test_engine):
    client = FakeEbayClient([load_fixture()])
    ingest(client=client, db_engine=test_engine)

    changed = load_fixture()
    changed["price"]["value"] = "199.99"
    client_second_pass = FakeEbayClient([changed])
    ingest(client=client_second_pass, db_engine=test_engine)

    with Session(test_engine) as session:
        rows = session.exec(select(Listing)).all()
        assert len(rows) == 1, "same source_id must update the existing row, not insert a second one"
        assert rows[0].price == 199.99

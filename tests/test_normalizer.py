import json
from pathlib import Path

from connectors.normalizer import normalize_ebay_item

FIXTURE = Path(__file__).parent / "fixtures" / "ebay_item_summary.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_normalize_ebay_item_maps_core_fields():
    listing = normalize_ebay_item(load_fixture())

    assert listing.source == "ebay"
    assert listing.source_id == "v1|123456789012|0"
    assert listing.title == "Nintendo Switch OLED Console - White"
    assert listing.price == 249.99
    assert listing.currency == "USD"
    assert listing.condition == "Used"
    assert listing.category == "Video Game Consoles"
    assert listing.url == "https://www.ebay.com/itm/123456789012"


def test_normalize_ebay_item_collects_all_images_in_order():
    listing = normalize_ebay_item(load_fixture())

    assert listing.images == [
        "https://i.ebayimg.com/images/g/main.jpg",
        "https://i.ebayimg.com/images/g/extra1.jpg",
        "https://i.ebayimg.com/images/g/extra2.jpg",
    ]


def test_normalize_ebay_item_builds_location_string():
    listing = normalize_ebay_item(load_fixture())

    assert listing.location == "Ithaca, NY, US"


def test_normalize_ebay_item_handles_missing_optional_fields():
    raw = load_fixture()
    del raw["additionalImages"]
    del raw["itemCreationDate"]
    raw["categories"] = []
    raw["itemLocation"] = {}

    listing = normalize_ebay_item(raw)

    assert listing.images == ["https://i.ebayimg.com/images/g/main.jpg"]
    assert listing.posted_at is None
    assert listing.category is None
    assert listing.location is None

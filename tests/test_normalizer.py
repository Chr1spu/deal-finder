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


# --- shipping and buying options -----------------------------------------
# See docs/decisions/0004-trustworthy-comp-data.md. Both come from fields
# already present in the payload ingestion fetches, so capturing them costs
# no extra API calls.


def test_shipping_cost_takes_the_cheapest_option():
    """A seller can list several shipping options. What matters for "what
    would this actually cost me" is the one a bargain hunter would pick."""
    listing = normalize_ebay_item(load_fixture())
    assert listing.shipping_cost == 12.50


def test_missing_shipping_is_none_not_zero():
    """None and 0.0 mean genuinely different things: unknown versus free.
    Collapsing unknown to zero would make an item look cheaper than it is, in
    exactly the comparison stage 4 depends on."""
    raw = load_fixture()
    del raw["shippingOptions"]
    assert normalize_ebay_item(raw).shipping_cost is None


def test_unparseable_shipping_cost_is_skipped_not_crashed():
    raw = load_fixture()
    raw["shippingOptions"] = [
        {"shippingCost": {"value": "not-a-number"}},
        {"shippingCost": {"value": "8.00"}},
    ]
    assert normalize_ebay_item(raw).shipping_cost == 8.00


def test_free_shipping_is_zero_not_none():
    raw = load_fixture()
    raw["shippingOptions"] = [{"shippingCost": {"value": "0.0"}}]
    assert normalize_ebay_item(raw).shipping_cost == 0.0


def test_auction_listing_is_flagged():
    """For an auction, price is the *current bid*, not an asking price, so
    stage 4 must be able to tell them apart before using one as a comp."""
    raw = load_fixture()
    raw["buyingOptions"] = ["AUCTION"]
    listing = normalize_ebay_item(raw)
    assert listing.is_auction is True
    assert listing.accepts_best_offer is False


def test_fixed_price_with_best_offer_is_flagged_but_not_an_auction():
    listing = normalize_ebay_item(load_fixture())
    assert listing.is_auction is False
    assert listing.accepts_best_offer is True


def test_missing_buying_options_defaults_to_neither():
    raw = load_fixture()
    del raw["buyingOptions"]
    listing = normalize_ebay_item(raw)
    assert listing.is_auction is False
    assert listing.accepts_best_offer is False


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

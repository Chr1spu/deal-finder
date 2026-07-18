import json
from pathlib import Path

from connectors.normalizer import enrich_from_item_body, normalize_ebay_item

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


# --- fields eBay already sends that used to be dropped --------------------
# See docs/decisions/0006-capture-what-ebay-already-sends.md.


def test_epid_is_captured():
    """eBay's own catalog id. Two listings sharing one are definitively the
    same product, which is what stage 3 only approximates."""
    assert normalize_ebay_item(load_fixture()).epid == "240012345"


def test_seller_quality_is_captured():
    listing = normalize_ebay_item(load_fixture())
    assert listing.seller_feedback_score == 4821
    assert listing.seller_feedback_percent == 99.4


def test_item_end_date_is_parsed():
    listing = normalize_ebay_item(load_fixture())
    assert listing.item_end_date is not None
    assert listing.item_end_date.year == 2026


def test_a_fixed_price_listing_with_no_end_date_is_gtc():
    """eBay omits itemEndDate for Good 'Til Cancelled listings, so its absence
    IS the signal. GTC listings auto-renew under new ids, which is how false
    'sales' get manufactured."""
    raw = load_fixture()
    del raw["itemEndDate"]
    assert normalize_ebay_item(raw).is_gtc is True


def test_an_auction_is_never_gtc():
    """Auctions always have an end date, so a missing one must not be read as
    GTC for them."""
    raw = load_fixture()
    del raw["itemEndDate"]
    raw["buyingOptions"] = ["AUCTION"]
    listing = normalize_ebay_item(raw)
    assert listing.is_auction is True
    assert listing.is_gtc is False


def test_a_listing_with_an_end_date_is_not_gtc():
    assert normalize_ebay_item(load_fixture()).is_gtc is False


def test_malformed_timestamps_do_not_break_ingest():
    """One oddly-formatted date must not fail a whole run."""
    raw = load_fixture()
    raw["itemEndDate"] = "not-a-date"
    raw["itemCreationDate"] = "also-not-a-date"
    listing = normalize_ebay_item(raw)
    assert listing.item_end_date is None
    assert listing.posted_at is None


def test_bid_count_is_captured():
    raw = load_fixture()
    raw["buyingOptions"] = ["AUCTION"]
    raw["bidCount"] = 14
    assert normalize_ebay_item(raw).bid_count == 14


def test_qualified_programs_are_captured():
    assert normalize_ebay_item(load_fixture()).qualified_programs == ["EBAY_PLUS"]


def test_itemsummary_has_no_aspects_or_sold_quantity():
    """Both only appear on a full getItem body, never on a search result."""
    listing = normalize_ebay_item(load_fixture())
    assert listing.aspects is None
    assert listing.sold_quantity is None


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


# --- enriching from the getItem body we already pay for -------------------


def _item_body(**overrides) -> dict:
    """A getItem response, which carries fields itemSummary never does."""
    body = load_fixture()
    body["localizedAspects"] = [
        {"type": "STRING", "name": "Brand", "value": "Nintendo"},
        {"type": "STRING", "name": "Model", "value": "Switch OLED"},
        {"type": "STRING", "name": "Storage Capacity", "value": "64 GB"},
    ]
    body["estimatedAvailabilities"] = [
        {"estimatedAvailabilityStatus": "IN_STOCK", "estimatedSoldQuantity": 7}
    ]
    body.update(overrides)
    return body


def test_enrich_flattens_aspects_to_a_plain_mapping():
    """localizedAspects is the structured Brand/Model data stage 3b plans to
    extract from titles with regex, handed over for free by a call the
    disappearance check already makes."""
    listing = normalize_ebay_item(load_fixture())
    enrich_from_item_body(listing, _item_body())

    assert listing.aspects == {
        "Brand": "Nintendo",
        "Model": "Switch OLED",
        "Storage Capacity": "64 GB",
    }


def test_enrich_reads_sold_quantity_from_a_list():
    listing = normalize_ebay_item(load_fixture())
    enrich_from_item_body(listing, _item_body())
    assert listing.sold_quantity == 7


def test_enrich_reads_sold_quantity_from_a_bare_object():
    """eBay's docs show this both ways and it hasn't been seen live, so both
    shapes are handled rather than one being guessed at."""
    listing = normalize_ebay_item(load_fixture())
    enrich_from_item_body(
        listing, _item_body(estimatedAvailabilities={"estimatedSoldQuantity": 3})
    )
    assert listing.sold_quantity == 3


def test_enrich_never_blanks_a_field_that_already_has_a_value():
    """A getItem response missing a field must not erase what ingestion
    already learned."""
    listing = normalize_ebay_item(load_fixture())
    listing.aspects = {"Brand": "Nintendo"}
    listing.sold_quantity = 5

    enrich_from_item_body(listing, {"itemId": "x"})

    assert listing.aspects == {"Brand": "Nintendo"}
    assert listing.sold_quantity == 5


def test_enrich_can_correct_a_gtc_guess():
    """If a listing looked GTC because search omitted the end date, but the
    full body has one, the guess should be corrected rather than kept."""
    raw = load_fixture()
    del raw["itemEndDate"]
    listing = normalize_ebay_item(raw)
    assert listing.is_gtc is True

    enrich_from_item_body(listing, _item_body())

    assert listing.is_gtc is False
    assert listing.item_end_date is not None


def test_enrich_ignores_empty_aspects():
    listing = normalize_ebay_item(load_fixture())
    enrich_from_item_body(listing, _item_body(localizedAspects=[]))
    assert listing.aspects is None


# --- delivered cost -------------------------------------------------------
# What a buyer pays is price + shipping. This matters most across sources: a
# Facebook pickup has zero shipping, so an eBay comp at 500 + 30 means the
# item is worth 530 delivered.
# See docs/decisions/0008-price-oracle-and-valuation-clients.md.


def test_total_cost_is_price_plus_shipping():
    listing = normalize_ebay_item(load_fixture())
    assert listing.price == 249.99
    assert listing.shipping_cost == 12.50
    assert listing.total_cost == 262.49


def test_total_cost_is_none_when_shipping_is_unknown():
    """Deliberately not falling back to price. Silently treating unknown
    shipping as free is the same bug relocated, and it biases in the dangerous
    direction by making items look cheaper than they are."""
    raw = load_fixture()
    del raw["shippingOptions"]
    listing = normalize_ebay_item(raw)

    assert listing.shipping_cost is None
    assert listing.total_cost is None


def test_total_cost_equals_price_when_shipping_is_free():
    """Free shipping is known information, unlike unknown shipping."""
    raw = load_fixture()
    raw["shippingOptions"] = [{"shippingCostType": "FIXED", "shippingCost": {"value": "0.0"}}]
    listing = normalize_ebay_item(raw)

    assert listing.total_cost == listing.price


def test_a_fixed_shipping_cost_is_not_flagged_as_estimated():
    assert normalize_ebay_item(load_fixture()).shipping_estimated is False


def test_a_calculated_only_shipping_cost_is_flagged_as_estimated():
    """CALCULATED shipping depends on the buyer's location, so the figure eBay
    returned may have been worked out for somewhere else entirely."""
    raw = load_fixture()
    raw["shippingOptions"] = [
        {"shippingCostType": "CALCULATED", "shippingCost": {"value": "18.00"}}
    ]
    listing = normalize_ebay_item(raw)

    assert listing.shipping_cost == 18.00
    assert listing.shipping_estimated is True


def test_a_fixed_option_wins_even_when_a_calculated_one_is_cheaper():
    """A firm price that's slightly higher is better information than a
    cheaper guess made for an unknown location."""
    raw = load_fixture()
    raw["shippingOptions"] = [
        {"shippingCostType": "CALCULATED", "shippingCost": {"value": "4.00"}},
        {"shippingCostType": "FIXED", "shippingCost": {"value": "9.00"}},
    ]
    listing = normalize_ebay_item(raw)

    assert listing.shipping_cost == 9.00
    assert listing.shipping_estimated is False


def test_a_missing_shipping_cost_type_is_treated_as_firm():
    """eBay uses FIXED for the common case, and an absent type is more likely
    a firm price than a location-dependent estimate."""
    raw = load_fixture()
    raw["shippingOptions"] = [{"shippingCost": {"value": "7.00"}}]
    listing = normalize_ebay_item(raw)

    assert listing.shipping_cost == 7.00
    assert listing.shipping_estimated is False

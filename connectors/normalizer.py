"""Normalizes per-source raw API responses into the shared Listing schema."""

from __future__ import annotations

from datetime import UTC, datetime

from api.models import Listing


def _cheapest_shipping_cost(raw: dict) -> tuple[float | None, bool]:
    """(cheapest shipping cost, whether it's only an estimate).

    Cheapest rather than first: a seller can list several options (economy,
    expedited), and what matters for "what would this actually cost me" is the
    one a bargain hunter would pick. None and 0.0 mean genuinely different
    things here (unknown vs. free shipping), so an absent or unparseable cost
    must not collapse to zero, which would silently make an item look cheaper
    than it is in exactly the comparison stage 4 cares about.

    FIXED options are preferred over CALCULATED ones even when the calculated
    figure is cheaper. A calculated cost depends on the buyer's location, so
    the number eBay returned may have been worked out for somewhere else
    entirely; a firm price that's slightly higher is better information than a
    cheaper guess. When only CALCULATED options exist the value is still
    recorded, flagged as estimated so price confidence can discount it.
    See docs/decisions/0008-price-oracle-and-valuation-clients.md.
    """
    fixed: list[float] = []
    estimated: list[float] = []

    for option in raw.get("shippingOptions") or []:
        value = (option.get("shippingCost") or {}).get("value")
        if value is None:
            continue
        try:
            cost = float(value)
        except (TypeError, ValueError):
            continue
        # Anything not explicitly CALCULATED is treated as firm. eBay uses
        # FIXED for the common case, and an unrecognised or missing type is
        # more likely a firm price than a location-dependent estimate.
        if option.get("shippingCostType") == "CALCULATED":
            estimated.append(cost)
        else:
            fixed.append(cost)

    if fixed:
        return min(fixed), False
    if estimated:
        return min(estimated), True
    return None, False


def _parse_ebay_datetime(value: str | None) -> datetime | None:
    """eBay sends UTC as a trailing Z, which fromisoformat didn't accept until
    3.11 and still won't for some of eBay's variants. Returns None rather than
    raising, so one oddly-formatted timestamp can't fail an ingest run."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _flatten_aspects(raw: dict) -> dict | None:
    """localizedAspects arrives as [{name, value, type}, ...] from getItem.

    Flattened to a plain name -> value mapping, which is what any consumer
    actually wants. Kept as raw JSON rather than parsed into typed columns:
    aspect names vary by category ("Model" here, "Chipset/GPU Model" there)
    and coverage is unknown, so stage 3b should design that mapping against
    real data. See docs/decisions/0006-capture-what-ebay-already-sends.md.
    """
    aspects = raw.get("localizedAspects")
    if not aspects:
        return None
    flattened = {
        a["name"]: a["value"] for a in aspects if isinstance(a, dict) and a.get("name") and a.get("value")
    }
    return flattened or None


def listing_has_ended(raw: dict, now: datetime | None = None) -> bool:
    """True when a getItem body describes a listing that has left the market.

    This exists because the original assumption behind disappearance tracking
    was simply wrong, and wrong in the direction that produces silence rather
    than errors. The check treated "get_item returned 404" as the sole sold
    signal. **eBay does not 404 ended listings.** Measured against production
    on 2026-07-25: of eight listings that had dropped out of search coverage,
    zero returned 404, four were plainly ended (OUT_OF_STOCK, or an
    itemEndDate in the past) and all four returned HTTP 200. Nothing would
    ever have been marked likely_sold, and nothing ever was.

    Two independent signals, either of which is sufficient:

    estimatedAvailabilityStatus == OUT_OF_STOCK. For the secondhand
    single-quantity listings this project tracks, out of stock means sold.
    (A multi-quantity seller can be temporarily out of stock while the
    listing stays up, which is the false positive to be aware of. The
    two-strike confirmation still applies on top of this, so a transient
    blip costs one extra call rather than a wrong comp.)

    itemEndDate in the past. Unambiguous: the listing's own scheduled end has
    been reached. Note this field appears only in getItem bodies, never in
    search results, which is the subject of the note in normalize_ebay_item.

    Deliberately conservative: an unparseable or absent field means "not known
    to have ended", so uncertainty leaves a listing active rather than
    inventing a sale. False negatives cost a later re-check; false positives
    would put a fabricated comp into the dataset permanently.
    """
    now = now or datetime.now(UTC)

    availabilities = raw.get("estimatedAvailabilities")
    candidates = (
        [availabilities]
        if isinstance(availabilities, dict)
        else [a for a in availabilities if isinstance(a, dict)]
        if isinstance(availabilities, list)
        else []
    )
    for availability in candidates:
        if availability.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK":
            return True
        if availability.get("estimatedAvailableQuantity") == 0:
            return True

    end_date = _parse_ebay_datetime(raw.get("itemEndDate"))
    return end_date is not None and end_date <= now


def enrich_from_item_body(listing: Listing, raw: dict) -> None:
    """Fill in fields that only a full getItem body carries, in place.

    The disappearance check already fetches this body for every listing it
    checks and used to read only the status code, so everything here is free.
    Deliberately additive: it only fills fields, never blanks one that already
    has a value, because a getItem response missing a field should not erase
    what ingestion previously learned.
    """
    aspects = _flatten_aspects(raw)
    if aspects:
        listing.aspects = aspects

    # Backfill the category id on rows ingested before it was captured. This
    # is how the existing corpus acquires one at all: nothing else revisits an
    # old row, and the id is what ml/similar.py should eventually filter on
    # instead of the locale-dependent name. Additive like everything here, so
    # a body that omits categories cannot blank a value already learned.
    if listing.category_id is None:
        categories = raw.get("categories") or []
        if categories and categories[0].get("categoryId"):
            listing.category_id = categories[0]["categoryId"]

    sold = _sold_quantity(raw)
    if sold is not None:
        listing.sold_quantity = sold

    if listing.epid is None and raw.get("epid"):
        listing.epid = raw["epid"]

    # A getItem body is the only place GTC can actually be determined, since
    # search never returns itemEndDate at all. Settle it definitively here,
    # in both directions: an end date means not GTC, and its absence from a
    # *full item body* on a non-auction listing is the real GTC marker that
    # docs/decisions/0006 was reaching for.
    end_date = _parse_ebay_datetime(raw.get("itemEndDate"))
    if end_date is not None:
        if listing.item_end_date is None:
            listing.item_end_date = end_date
        listing.is_gtc = False
    elif not listing.is_auction:
        listing.is_gtc = True

    if isinstance(raw.get("bidCount"), int):
        listing.bid_count = raw["bidCount"]


def _sold_quantity(raw: dict) -> int | None:
    """Units eBay reports as sold, from a getItem body.

    estimatedAvailabilities is documented as an array of EstimatedAvailability
    on the Item type, but appears as a bare object in places, and this hasn't
    been seen against a live response yet (the quota was exhausted when it was
    written). Handle both rather than pick one and be silently wrong: guessing
    the wrong shape would leave this null forever with nothing to notice.
    """
    availabilities = raw.get("estimatedAvailabilities")
    if isinstance(availabilities, dict):
        candidates = [availabilities]
    elif isinstance(availabilities, list):
        candidates = [a for a in availabilities if isinstance(a, dict)]
    else:
        return None

    quantities = [
        a["estimatedSoldQuantity"]
        for a in candidates
        if isinstance(a.get("estimatedSoldQuantity"), int)
    ]
    return max(quantities) if quantities else None


def normalize_ebay_item(raw: dict) -> Listing:
    """Map a raw eBay Browse API itemSummary into a Listing (not yet persisted)."""

    images = []
    if raw.get("image", {}).get("imageUrl"):
        images.append(raw["image"]["imageUrl"])
    images += [img["imageUrl"] for img in raw.get("additionalImages", []) if img.get("imageUrl")]

    location_parts = [
        raw.get("itemLocation", {}).get("city"),
        raw.get("itemLocation", {}).get("stateOrProvince"),
        raw.get("itemLocation", {}).get("country"),
    ]
    location = ", ".join(p for p in location_parts if p) or None

    categories = raw.get("categories", [])
    category = categories[0]["categoryName"] if categories else None
    # The id, not just the name. categoryName is locale-dependent and the id
    # is not, which is the whole reason this field exists. See api/models.py.
    category_id = categories[0].get("categoryId") if categories else None

    posted_at = _parse_ebay_datetime(raw.get("itemCreationDate"))
    item_end_date = _parse_ebay_datetime(raw.get("itemEndDate"))

    seller = raw.get("seller") or {}
    feedback_percent = seller.get("feedbackPercentage")

    # For an AUCTION, raw["price"] is the *current bid*, not an asking price.
    # It's still recorded, but is_auction is what lets stage 4 avoid treating
    # a mid-auction snapshot as a comparable sale.
    # See docs/decisions/0004-trustworthy-comp-data.md.
    buying_options = raw.get("buyingOptions") or []
    is_auction = "AUCTION" in buying_options
    shipping_cost, shipping_estimated = _cheapest_shipping_cost(raw)

    # is_gtc is deliberately left UNKNOWN (None) here, and this reverses what
    # docs/decisions/0006 claimed. That ADR reasoned "eBay omits itemEndDate
    # for Good 'Til Cancelled listings, so a missing end date is the GTC
    # marker". True of a getItem body, false of a search response: measured
    # 2026-07-25, itemEndDate is returned by getItem and is *never* present in
    # an itemSummary, on any listing. Deriving GTC from its absence here
    # therefore marked every non-auction listing GTC and measured 98.9%, which
    # was not a finding about eBay but an artefact of asking the wrong
    # endpoint. Only enrich_from_item_body can settle this.
    is_gtc = None

    return Listing(
        source="ebay",
        source_id=raw["itemId"],
        title=raw["title"],
        price=float(raw["price"]["value"]),
        currency=raw["price"].get("currency", "USD"),
        shipping_cost=shipping_cost,
        shipping_estimated=shipping_estimated,
        is_auction=is_auction,
        accepts_best_offer="BEST_OFFER" in buying_options,
        epid=raw.get("epid"),
        item_end_date=item_end_date,
        is_gtc=is_gtc,
        bid_count=raw.get("bidCount"),
        seller_feedback_score=seller.get("feedbackScore"),
        seller_feedback_percent=float(feedback_percent) if feedback_percent is not None else None,
        qualified_programs=raw.get("qualifiedPrograms") or None,
        # Only present on getItem responses, not itemSummary. Populated when
        # the disappearance check passes a full item body through.
        aspects=_flatten_aspects(raw),
        sold_quantity=_sold_quantity(raw),
        images=images,
        location=location,
        condition=raw.get("condition"),
        category=category,
        category_id=category_id,
        url=raw["itemWebUrl"],
        posted_at=posted_at,
    )

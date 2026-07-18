"""Normalizes per-source raw API responses into the shared Listing schema."""

from __future__ import annotations

from datetime import datetime

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

    sold = _sold_quantity(raw)
    if sold is not None:
        listing.sold_quantity = sold

    if listing.epid is None and raw.get("epid"):
        listing.epid = raw["epid"]

    if listing.item_end_date is None:
        end_date = _parse_ebay_datetime(raw.get("itemEndDate"))
        if end_date is not None:
            listing.item_end_date = end_date
            listing.is_gtc = False

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

    # eBay omits itemEndDate for Good 'Til Cancelled listings, so a missing
    # end date on a fixed-price listing *is* the GTC marker. That matters
    # because GTC listings auto-renew under new item ids, which is how false
    # "sales" get manufactured (see docs/decisions/0005-sale-confidence.md).
    # Auctions always have an end date, so they're never GTC.
    is_gtc = item_end_date is None and not is_auction

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
        url=raw["itemWebUrl"],
        posted_at=posted_at,
    )

"""Normalizes per-source raw API responses into the shared Listing schema."""

from __future__ import annotations

from datetime import datetime

from api.models import Listing


def _cheapest_shipping_cost(raw: dict) -> float | None:
    """Lowest shipping cost across the offered options, or None if unknown.

    Cheapest rather than first: a seller can list several options (economy,
    expedited), and what matters for "what would this actually cost me" is the
    one a bargain hunter would pick. None and 0.0 mean genuinely different
    things here (unknown vs. free shipping), so an absent or unparseable cost
    must not collapse to zero, which would silently make an item look cheaper
    than it is in exactly the comparison stage 4 cares about.
    """
    costs = []
    for option in raw.get("shippingOptions") or []:
        value = (option.get("shippingCost") or {}).get("value")
        if value is None:
            continue
        try:
            costs.append(float(value))
        except (TypeError, ValueError):
            continue
    return min(costs) if costs else None


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

    posted_at = None
    if raw.get("itemCreationDate"):
        posted_at = datetime.fromisoformat(raw["itemCreationDate"].replace("Z", "+00:00"))

    # For an AUCTION, raw["price"] is the *current bid*, not an asking price.
    # It's still recorded, but is_auction is what lets stage 4 avoid treating
    # a mid-auction snapshot as a comparable sale.
    # See docs/decisions/0004-trustworthy-comp-data.md.
    buying_options = raw.get("buyingOptions") or []

    return Listing(
        source="ebay",
        source_id=raw["itemId"],
        title=raw["title"],
        price=float(raw["price"]["value"]),
        currency=raw["price"].get("currency", "USD"),
        shipping_cost=_cheapest_shipping_cost(raw),
        is_auction="AUCTION" in buying_options,
        accepts_best_offer="BEST_OFFER" in buying_options,
        images=images,
        location=location,
        condition=raw.get("condition"),
        category=category,
        url=raw["itemWebUrl"],
        posted_at=posted_at,
    )

"""Normalizes per-source raw API responses into the shared Listing schema."""

from __future__ import annotations

from datetime import datetime

from api.models import Listing


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

    return Listing(
        source="ebay",
        source_id=raw["itemId"],
        title=raw["title"],
        price=float(raw["price"]["value"]),
        currency=raw["price"].get("currency", "USD"),
        images=images,
        location=location,
        condition=raw.get("condition"),
        category=category,
        url=raw["itemWebUrl"],
        posted_at=posted_at,
    )

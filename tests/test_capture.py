import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

from api.models import EMBEDDING_DIM, Listing, PriceObservation
from connectors.capture import CapturedListing, save_capture, to_listing
from connectors.disappearance_check import COMP_SOURCES, PULL_BASED_SOURCES


def payload(**overrides) -> CapturedListing:
    fields: dict = {
        "source": "depop",
        "source_id": "depop-123",
        "title": "Nike Air Max 90 white size 10",
        "price": 65.0,
        "url": "https://www.depop.com/products/seller-nike-air-max/",
        "images": ["https://media-photos.depop.com/b1/1/photo.jpg"],
        "brand": "Nike",
        "size": "10",
        "condition": "Used",
    }
    fields.update(overrides)
    return CapturedListing.model_validate(fields)


# ------------------------------------------------------------- validation


def test_an_unknown_source_is_rejected():
    """A closed set, not free text. An extension bug sending 'Depop' or
    'depop.com' would create rows no query filters on, and nothing would
    error."""
    with pytest.raises(ValidationError, match="source must be one of"):
        payload(source="craigslist")


def test_ebay_cannot_be_captured():
    """eBay comes in through its official API. Letting it arrive by capture
    would put unverified rows into the one source that builds price history."""
    with pytest.raises(ValidationError):
        payload(source="ebay")


def test_source_is_normalized_to_lowercase():
    assert payload(source="Depop").source == "depop"


def test_a_zero_price_is_rejected():
    """A page-parse failure shows up as 0 far more often than as a missing
    field, so this catches the common breakage rather than storing junk."""
    with pytest.raises(ValidationError, match="price must be positive"):
        payload(price=0)


def test_a_blank_title_is_rejected():
    with pytest.raises(ValidationError):
        payload(title="   ")


def test_optional_fields_may_all_be_absent():
    """These pages get parsed from markup that changes without notice. A
    capture with no size and no condition is still worth keeping."""
    captured = CapturedListing(
        source="facebook",
        source_id="fb-1",
        title="Desk chair",
        price=40.0,
        url="https://www.facebook.com/marketplace/item/1/",
    )
    assert captured.images == []
    assert captured.brand is None


# ---------------------------------------------------------- normalization


def test_structured_attributes_land_in_the_shared_aspects_column():
    """Depop's brand/size go where eBay's localizedAspects go, so stage 3b
    reads one shape instead of branching per source."""
    listing = to_listing(payload())
    assert listing.aspects == {"Brand": "Nike", "Size": "10"}


def test_captured_listings_are_never_auctions_or_gtc():
    listing = to_listing(payload())
    assert listing.is_auction is False
    assert listing.is_gtc is False


def test_unknown_shipping_stays_unknown():
    """None is not zero. Listing.total_cost refuses to conflate them, and a
    captured listing with unknown shipping must not look free."""
    listing = to_listing(payload())
    assert listing.shipping_cost is None
    assert listing.total_cost is None


def test_facebook_pickup_is_genuinely_zero_shipping():
    listing = to_listing(payload(source="facebook", shipping_cost=0.0, price=450.0))
    assert listing.total_cost == 450.0, "local pickup really is the full cost"


# --------------------------------------------------------------- persistence


def fake_hasher(url: str) -> str:
    return f"hash-of-{url}"


def test_capture_inserts_and_records_an_opening_price(test_engine):
    listing, created = save_capture(payload(), db_engine=test_engine, image_hasher=fake_hasher)

    assert created is True
    assert listing.source == "depop"
    with Session(test_engine) as session:
        observations = session.exec(select(PriceObservation)).all()
        assert len(observations) == 1
        assert observations[0].price == 65.0


def test_recapturing_updates_rather_than_duplicating(test_engine):
    save_capture(payload(), db_engine=test_engine, image_hasher=fake_hasher)
    listing, created = save_capture(payload(price=55.0), db_engine=test_engine, image_hasher=fake_hasher)

    assert created is False
    assert listing.price == 55.0
    with Session(test_engine) as session:
        assert len(session.exec(select(Listing)).all()) == 1
        prices = [o.price for o in session.exec(select(PriceObservation)).all()]
        assert sorted(prices) == [55.0, 65.0], "the drop is recorded, not overwritten"


def test_recapture_at_the_same_price_records_nothing_new(test_engine):
    save_capture(payload(), db_engine=test_engine, image_hasher=fake_hasher)
    save_capture(payload(), db_engine=test_engine, image_hasher=fake_hasher)

    with Session(test_engine) as session:
        assert len(session.exec(select(PriceObservation)).all()) == 1


def test_a_changed_photo_forces_re_embedding(test_engine):
    """The embedding describes the photo. If the seller swaps it, the stored
    vector is describing something that is no longer there."""
    listing, _ = save_capture(payload(), db_engine=test_engine, image_hasher=fake_hasher)
    with Session(test_engine) as session:
        row = session.get(Listing, listing.id)
        row.embedding = [0.1] * EMBEDDING_DIM
        row.embedded_at = row.first_seen_at
        session.add(row)
        session.commit()

    save_capture(
        payload(images=["https://media-photos.depop.com/b1/1/different.jpg"]),
        db_engine=test_engine,
        image_hasher=fake_hasher,
    )

    with Session(test_engine) as session:
        row = session.get(Listing, listing.id)
        assert row.embedded_at is None, "must be re-embedded against the new photo"
        assert row.embedding is None
        assert row.image_hash == "hash-of-https://media-photos.depop.com/b1/1/different.jpg"


def test_capture_hashes_the_primary_image(test_engine):
    """Without this, ml.match's cheap exact first pass is dead code for every
    captured listing, which is backwards: a reused stock photo is the highest
    precision cross-source signal there is, and reuse is far more common off
    eBay than on it."""
    listing, _ = save_capture(payload(), db_engine=test_engine, image_hasher=fake_hasher)

    assert listing.image_hash == "hash-of-https://media-photos.depop.com/b1/1/photo.jpg"


def test_a_capture_with_no_images_has_no_hash(test_engine):
    listing, _ = save_capture(
        payload(images=[]), db_engine=test_engine, image_hasher=fake_hasher
    )
    assert listing.image_hash is None


# ------------------------------------------------- architectural guardrails


def test_captured_sources_are_never_polled_or_used_as_comps():
    """The structural guarantee behind ADR 0008 and 0010. Depop and Facebook
    listings get *scored against* eBay value; they must never be polled for
    disappearance (there is no reliable signal) nor contribute comps (item
    variety makes their price history meaningless)."""
    for source in ("depop", "facebook"):
        assert source not in PULL_BASED_SOURCES, f"{source} must not be polled"
        assert source not in COMP_SOURCES, f"{source} must never become a comp source"


def test_depop_has_no_pull_based_client():
    """Depop returns 403 to every server-side request behind Cloudflare Bot
    Management, so a DepopClient cannot exist and must not be reintroduced.
    See docs/decisions/0010-depop-is-push-based-now.md."""
    import connectors

    assert not hasattr(connectors, "depop"), "Depop is push-based; there is no connector"

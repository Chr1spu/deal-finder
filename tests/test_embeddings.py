import io

import httpx
import pytest
import respx
from PIL import Image

from api.models import EMBEDDING_DIM
from ml.embeddings import (
    CLIP_MODEL,
    IMAGE_SIZE_VARIANT,
    embed_image_urls,
    fetch_image,
    upgrade_image_url,
)


def png_bytes(color: str = "red", size: tuple[int, int] = (64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def fake_image() -> Image.Image:
    return Image.new("RGB", (64, 64), "blue")


def fake_embedder(images, device=None):
    """Stands in for the CLIP forward pass. No torch, no GPU."""
    return [[0.5] * EMBEDDING_DIM for _ in images]


def always_fetches(url, *_args):
    return fake_image()


# ---------------------------------------------------------------- URL variant


def test_ebay_thumbnail_url_is_upgraded_to_a_larger_variant():
    url = "https://i.ebayimg.com/images/g/abc/s-l225.jpg"
    assert upgrade_image_url(url) == f"https://i.ebayimg.com/images/g/abc/{IMAGE_SIZE_VARIANT}.jpg"


def test_a_url_that_is_not_an_ebay_size_variant_passes_through_untouched():
    """A Depop or Facebook image URL must not be mangled by a rule that only
    describes eBay's CDN."""
    for url in (
        "https://media-photos.depop.com/b1/123/photo.jpg",
        "https://i.ebayimg.com/images/g/abc/main.jpg",
        "https://example.com/s-l225-but-not-at-the-end.jpg/other.png",
    ):
        assert upgrade_image_url(url) == url


# ------------------------------------------------------------------ fetching


@respx.mock
def test_fetch_image_returns_a_decoded_image():
    target = f"https://i.ebayimg.com/images/g/abc/{IMAGE_SIZE_VARIANT}.jpg"
    respx.get(target).mock(return_value=httpx.Response(200, content=png_bytes()))

    image = fetch_image("https://i.ebayimg.com/images/g/abc/s-l225.jpg")

    assert image is not None
    assert image.size == (64, 64)
    assert image.mode == "RGB", "CLIP's transform expects three channels"


@respx.mock
def test_fetch_image_returns_none_on_a_dead_link():
    """One dead photo must not fail a run over ten thousand listings, the same
    contract connectors/image_hash.fetch_and_hash has."""
    target = f"https://i.ebayimg.com/images/g/gone/{IMAGE_SIZE_VARIANT}.jpg"
    respx.get(target).mock(return_value=httpx.Response(404))

    assert fetch_image("https://i.ebayimg.com/images/g/gone/s-l225.jpg") is None


@respx.mock
def test_fetch_image_returns_none_on_bytes_that_are_not_an_image():
    target = f"https://i.ebayimg.com/images/g/junk/{IMAGE_SIZE_VARIANT}.jpg"
    respx.get(target).mock(return_value=httpx.Response(200, content=b"not an image at all"))

    assert fetch_image("https://i.ebayimg.com/images/g/junk/s-l225.jpg") is None


# ------------------------------------------------------- position preservation


def test_embed_image_urls_preserves_position_across_a_failed_fetch():
    """The most important test in this file.

    Callers zip the result against listing ids, so dropping a failure instead
    of padding it would shift every later vector up by one: listing N would
    silently receive listing N+1's embedding, and nothing would raise. The
    failure sits in the middle on purpose, since one at the end would pass
    even with the buggy behaviour.
    """

    def fetcher(url, *_args):
        return None if url == "dead.jpg" else fake_image()

    vectors = embed_image_urls(
        ["a.jpg", "dead.jpg", "c.jpg", "d.jpg"], fetcher=fetcher, embedder=fake_embedder
    )

    assert len(vectors) == 4, "one entry per input, always"
    assert vectors[1] is None, "the failure keeps its slot"
    assert [v is not None for v in vectors] == [True, False, True, True]


def test_embed_image_urls_keeps_a_none_url_in_place():
    """A listing with no images at all still occupies a position."""
    vectors = embed_image_urls(
        ["a.jpg", None, "c.jpg"], fetcher=always_fetches, embedder=fake_embedder
    )

    assert len(vectors) == 3
    assert vectors[1] is None
    assert vectors[0] is not None and vectors[2] is not None


def test_embed_image_urls_batches_but_still_returns_every_position():
    calls: list[int] = []

    def counting_embedder(images, device=None):
        calls.append(len(images))
        return fake_embedder(images, device)

    vectors = embed_image_urls(
        [f"{i}.jpg" for i in range(5)],
        batch_size=2,
        fetcher=always_fetches,
        embedder=counting_embedder,
    )

    assert calls == [2, 2, 1], "batched, and the remainder is not dropped"
    assert len(vectors) == 5
    assert all(v is not None for v in vectors)


def test_a_wrong_dimension_vector_fails_loudly():
    """A checkpoint that disagrees with the column must fail here, not write
    garbage that only surfaces as bad search results weeks later."""

    def wrong_size(images, device=None):
        return [[0.5] * 768 for _ in images]

    with pytest.raises(ValueError, match="768"):
        embed_image_urls(["a.jpg"], fetcher=always_fetches, embedder=wrong_size)


# ------------------------------------------------------------- the real model


def test_the_real_checkpoint_produces_normalized_vectors_of_the_right_size():
    """Skipped unless the ml group is installed. Guards the one thing the
    fakes above cannot: that CLIP_MODEL actually yields EMBEDDING_DIM values,
    and that they are L2-normalized, so cosine distance means what the schema
    assumes it means."""
    pytest.importorskip("torch")
    pytest.importorskip("open_clip")

    from ml.embeddings import embed_images

    vectors = embed_images([fake_image(), Image.new("RGB", (32, 32), "green")])

    assert len(vectors) == 2
    for vector in vectors:
        assert len(vector) == EMBEDDING_DIM, f"{CLIP_MODEL} disagrees with the schema"
        norm = sum(component * component for component in vector) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-3)

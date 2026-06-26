import io

import httpx
import respx
from PIL import Image, ImageDraw

from connectors.image_hash import fetch_and_hash

IMAGE_URL = "https://example.test/photo.jpg"


def make_png_bytes(color: tuple[int, int, int]) -> bytes:
    """A flat-color square. Perceptual hashing looks at structure, not raw
    color, so this is fine for "does hashing work at all" but two different
    flat colors will hash identically, on purpose, not a bug."""
    image = Image.new("RGB", (32, 32), color=color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def make_structured_png_bytes(variant: str) -> bytes:
    """Two visibly different shapes, for asserting perceptual hashes
    actually differ between structurally different images."""
    image = Image.new("RGB", (64, 64), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)
    if variant == "diagonal":
        draw.line([(0, 0), (63, 63)], fill=(0, 0, 0), width=6)
    else:
        draw.ellipse([(4, 40), (60, 60)], fill=(0, 0, 0))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


@respx.mock
def test_fetch_and_hash_returns_a_hash_for_a_valid_image():
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=make_png_bytes((255, 0, 0))))

    result = fetch_and_hash(IMAGE_URL)

    assert result is not None
    assert isinstance(result, str)


@respx.mock
def test_same_image_hashes_identically():
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=make_png_bytes((10, 20, 30))))

    first = fetch_and_hash(IMAGE_URL)
    second = fetch_and_hash(IMAGE_URL)

    assert first == second


@respx.mock
def test_structurally_different_images_hash_differently():
    respx.get(IMAGE_URL).mock(
        return_value=httpx.Response(200, content=make_structured_png_bytes("diagonal"))
    )
    diagonal_hash = fetch_and_hash(IMAGE_URL)

    respx.get(IMAGE_URL).mock(
        return_value=httpx.Response(200, content=make_structured_png_bytes("ellipse"))
    )
    ellipse_hash = fetch_and_hash(IMAGE_URL)

    assert diagonal_hash != ellipse_hash


@respx.mock
def test_returns_none_on_404():
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(404))

    assert fetch_and_hash(IMAGE_URL) is None


@respx.mock
def test_returns_none_on_corrupt_image_bytes():
    respx.get(IMAGE_URL).mock(return_value=httpx.Response(200, content=b"not an image"))

    assert fetch_and_hash(IMAGE_URL) is None

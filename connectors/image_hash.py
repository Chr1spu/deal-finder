"""Perceptual image hashing for dedup (see docs/decisions/0002-image-hash-dedup.md).
Compute-only for now: no cross-source matching yet, that lands with Depop.
"""

from __future__ import annotations

import io

import httpx
import imagehash
from PIL import Image


def fetch_and_hash(url: str, http_client: httpx.Client | None = None) -> str | None:
    """Downloads the image at `url` and returns its perceptual hash as a
    string, or None if the fetch or decode fails. A single bad photo (dead
    link, corrupt image, timeout) shouldn't fail the whole ingest run."""
    client = http_client or httpx
    try:
        resp = client.get(url, timeout=10.0)
        resp.raise_for_status()
        image = Image.open(io.BytesIO(resp.content))
        return str(imagehash.phash(image))
    except (httpx.HTTPError, OSError):
        return None

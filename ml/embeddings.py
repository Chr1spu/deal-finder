"""CLIP image embeddings: image in, vector out. No database access.

Same layering as connectors/image_hash.py, which this deliberately mirrors:
pure compute here, and everything that owns a Session in ml/embed_listings.py.

Why this exists, since it isn't the obvious reason. eBay is the sole price
oracle (docs/decisions/0008), so this is not a general similarity feature.
The eBay corpus is a *reference index*, and listings found on Depop or
Facebook Marketplace are *queries* against it. A foreign listing carries no
eBay catalog id, so `epid` (exact, free, and better than any model for
eBay-internal comps) cannot bridge sources at all. Image embeddings and text
extraction are the only available bridge.

That asymmetry is worth keeping in mind: a missing eBay embedding removes a
possible match for every future query; a missing Depop one costs one lookup.

See docs/decisions/0009-clip-embeddings-pgvector.md.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import httpx

from api.models import EMBEDDING_DIM
from api.settings import settings
from systems.ratelimit import call_with_backoff

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from PIL.Image import Image

logger = logging.getLogger(__name__)

# A module constant, not a setting. It has to agree with the dimension the
# migration created, and a .env key that can silently disagree with the schema
# is a footgun. Same reasoning as NON_NEW_CONDITION_IDS in connectors/ebay.py.
#
# LAION's ViT-B/32 rather than OpenAI's original: same architecture and
# dimension, trained on a much larger public dataset, and open_clip's
# create_model_and_transforms returns the matching preprocessing transform
# alongside it, so normalization constants can't silently drift apart from the
# checkpoint. Swapping to SigLIP later is this one string plus a migration.
CLIP_MODEL = "hf-hub:laion/CLIP-ViT-B-32-laion2B-s34B-b79K"

# eBay's CDN serves size variants by URL substitution, at no API cost (the CDN
# is not the Browse API). Ingest stores the s-l225 thumbnail, which is smaller
# than CLIP's 224px input once aspect ratio is accounted for, so upscaling it
# would throw away detail the CDN will simply hand over. s-l500 is ~81 KB
# against ~17 KB, which is the right trade for a one-time backfill.
IMAGE_SIZE_VARIANT = "s-l500"
_SIZE_VARIANT_RE = re.compile(r"/s-l\d+\.(jpg|jpeg|png|webp)$", re.IGNORECASE)


def upgrade_image_url(url: str, variant: str = IMAGE_SIZE_VARIANT) -> str:
    """Rewrite an eBay CDN thumbnail URL to a larger variant.

    Anything that doesn't look like an eBay size-suffixed URL is returned
    untouched, so a Depop or Facebook image URL passes straight through
    rather than being mangled by a rule that doesn't apply to it.
    """
    return _SIZE_VARIANT_RE.sub(lambda m: f"/{variant}.{m.group(1)}", url)


def fetch_image(url: str, http_client: httpx.Client | None = None) -> Image | None:
    """Download an image and decode it, or None if either step fails.

    Same failure contract as connectors/image_hash.fetch_and_hash: one dead
    link, corrupt file or timeout must not fail a run over ten thousand
    listings. Wrapped in call_with_backoff so a transient 429/5xx from the CDN
    is retried rather than silently costing an embedding.
    """
    from PIL import Image as PILImage

    client = http_client or httpx
    target = upgrade_image_url(url)

    def do_request() -> httpx.Response:
        resp = client.get(target, timeout=20.0, follow_redirects=True)
        resp.raise_for_status()
        return resp

    try:
        resp = call_with_backoff(do_request)
        # convert("RGB") because CLIP's transform expects three channels and
        # eBay serves the occasional palettized PNG or CMYK JPEG.
        return PILImage.open(io.BytesIO(resp.content)).convert("RGB")
    except (httpx.HTTPError, OSError, ValueError):
        logger.debug("could not fetch or decode image %s", target, exc_info=True)
        return None


def resolve_device(configured: str | None = None) -> str:
    """"auto" picks cuda when torch reports it, else cpu."""
    import torch

    choice = configured or settings.embedding_device
    if choice != "auto":
        return choice
    return "cuda" if torch.cuda.is_available() else "cpu"


@lru_cache(maxsize=1)
def _load_model(device: str) -> tuple[Any, Any]:
    """Load the checkpoint once per process and keep it resident.

    Cached because loading is seconds and hundreds of megabytes, while a job
    embeds in chunks and the scheduler enqueues a new job every few minutes.
    This holds only because systems.queue.WindowsWorker is a SimpleWorker,
    which does not fork per job: a forking worker would discard this cache
    every time, and forking with an initialized CUDA context is its own hazard.
    """
    import open_clip

    logger.info("loading CLIP model %s on %s", CLIP_MODEL, device)
    model, _, preprocess = open_clip.create_model_and_transforms(CLIP_MODEL, device=device)
    model.eval()
    return model, preprocess


def embed_images(images: list[Image], device: str | None = None) -> list[list[float]]:
    """Embed already-decoded images. One vector per input, same order.

    Vectors are L2-normalized here, at write time, so cosine distance is
    meaningful in the database and `1 - (a <=> b)` lands in [0, 1] for the
    stage 4 match-confidence score.
    """
    import torch

    if not images:
        return []

    resolved = resolve_device(device)
    model, preprocess = _load_model(resolved)

    batch = torch.stack([preprocess(image) for image in images]).to(resolved)
    with torch.no_grad():
        features = model.encode_image(batch)
        features /= features.norm(dim=-1, keepdim=True)

    return [vector.tolist() for vector in features.cpu().float()]


def embed_image_urls(
    urls: Iterable[str | None],
    http_client: httpx.Client | None = None,
    batch_size: int | None = None,
    fetcher: Callable[..., Image | None] = fetch_image,
    embedder: Callable[..., list[list[float]]] = embed_images,
    device: str | None = None,
) -> list[list[float] | None]:
    """Fetch and embed each URL. **One entry per input, in input order, None
    where the fetch failed.**

    Position-preserving is the whole contract of this function. Callers zip
    the result against listing ids, so silently dropping a failure would
    mis-assign every vector after the first bad photo: listing N would get
    listing N+1's embedding, and nothing would raise. Hence the explicit None
    padding and the test that fails a middle element specifically.

    Raises ValueError on a vector of unexpected length, so a checkpoint that
    disagrees with the column fails loudly here rather than writing garbage
    that only shows up as bad search results weeks later.

    fetcher and embedder are injectable, the same way connectors take a
    `client` and an `image_hasher`, so the ordering guarantee above can be
    tested without a network or a GPU.
    """
    size = batch_size or settings.embedding_batch_size
    urls = list(urls)
    results: list[list[float] | None] = [None] * len(urls)

    # Fetch and embed in batches so GPU work is batched and memory stays
    # bounded, while positions are tracked explicitly rather than inferred.
    pending: list[tuple[int, Image]] = []

    def flush() -> None:
        if not pending:
            return
        vectors = embedder([image for _, image in pending], device=device)
        for (position, _), vector in zip(pending, vectors, strict=True):
            if len(vector) != EMBEDDING_DIM:
                raise ValueError(
                    f"{CLIP_MODEL} produced a {len(vector)}-dim vector, "
                    f"but the schema expects {EMBEDDING_DIM}"
                )
            results[position] = vector
        pending.clear()

    for position, url in enumerate(urls):
        if not url:
            continue
        image = fetcher(url, http_client) if http_client is not None else fetcher(url)
        if image is None:
            continue
        pending.append((position, image))
        if len(pending) >= size:
            flush()

    flush()
    return results

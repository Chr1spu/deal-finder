"""Embed listings that don't have a vector yet.

Backfill and go-forward are deliberately the same code path. `embed_pending`
selects `WHERE embedded_at IS NULL`, which is simultaneously the existing
corpus and every row ingestion lands from now on, so there is no separate
one-off backfill script to keep in sync with the real thing.

Not run inline in ingest_saved_search, which is the opposite call from
image_hash (docs/decisions/0002). The contrast is the point: hashing is cheap
CPU work with no heavy dependency, embedding is neither. Inline would put
torch in the ingest worker's process, let a CUDA fault take down data
ingestion rather than just a feature job, add GPU work to a run that is
already ~30 minutes and sequential, and force batch size 1.

See docs/decisions/0009-clip-embeddings-pgvector.md.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from api.db import engine as default_engine
from api.models import Listing
from ml.embeddings import embed_image_urls

logger = logging.getLogger(__name__)

Embedder = Callable[..., list["list[float] | None"]]

# How many listings to load and commit at a time. Chunked rather than loading
# the table: this is a database paging concern, not a VRAM one (the GPU batch
# size is separate and much smaller). Committing per chunk is what makes a run
# resume-safe, so a crash at listing 7,000 keeps the first 7,000.
DEFAULT_CHUNK_SIZE = 500


@dataclass
class EmbedResult:
    """attempted and embedded differ by the listings whose image could not be
    fetched or decoded. Tracked separately because a large and growing gap
    means dead image URLs, not a broken model, and the two need different
    responses."""

    attempted: int = 0
    embedded: int = 0

    @property
    def failed(self) -> int:
        return self.attempted - self.embedded


# Postgres aborts one side of a deadlock rather than blocking forever, so a
# deadlock is a normal outcome under concurrency and the standard remedy is to
# retry. 40P01 is deadlock_detected, 40001 serialization_failure.
_RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})
_WRITE_ATTEMPTS = 4


def _write_embeddings(
    db_engine: Engine, updates: list[tuple[int | None, list[float] | None]]
) -> int:
    """Persist one chunk of vectors, retrying if Postgres aborts us.

    Returns how many rows got an actual vector. `embedded_at` is stamped on
    every row either way: a listing with no image, or whose image 404s, is done
    being asked, and keying the work queue on `embedded_at` rather than
    `embedding IS NULL` is what stops those being retried forever.

    Retries re-open the session and re-fetch, because a deadlock aborts the
    whole transaction. It does NOT recompute the vectors, which are the
    expensive part and are already in hand.
    """
    embedded = 0
    for attempt in range(1, _WRITE_ATTEMPTS + 1):
        embedded = 0
        try:
            with Session(db_engine) as session:
                now = datetime.now(UTC)
                for listing_id, vector in updates:
                    listing = session.get(Listing, listing_id)
                    if listing is None:
                        continue
                    listing.embedded_at = now
                    if vector is not None:
                        listing.embedding = vector
                        embedded += 1
                    session.add(listing)
                session.commit()
            return embedded
        except OperationalError as error:
            sqlstate = getattr(error.orig, "sqlstate", None)
            if sqlstate not in _RETRYABLE_SQLSTATES or attempt == _WRITE_ATTEMPTS:
                raise
            # Back off a little so both sides do not retry in lockstep.
            delay = 0.5 * attempt
            logger.warning(
                "embedding write hit %s, retrying in %.1fs (attempt %d/%d)",
                sqlstate, delay, attempt, _WRITE_ATTEMPTS,
            )
            time.sleep(delay)
    return embedded


def embed_pending(
    limit: int | None = None,
    db_engine: Engine | None = None,
    embedder: Embedder = embed_image_urls,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> EmbedResult:
    """Embed listings with no embedded_at, oldest id first.

    Idempotent and resume-safe. `embedded_at` is stamped on every attempt,
    success or failure, which is why the queue keys on it rather than on
    `embedding IS NULL`: the latter would retry every imageless listing and
    every dead image URL on every run, forever, and those never succeed.

    embedder is injectable so tests exercise the paging, stamping and commit
    logic without a GPU or a network.
    """
    db_engine = db_engine or default_engine
    result = EmbedResult()

    # One HTTP client for the whole run. At ~10,000 images the TCP+TLS
    # handshake per request dominates, which is the same lesson ingest_all
    # learned when its first full run took 30-35 minutes.
    owns_http = embedder is embed_image_urls
    http_client = httpx.Client(timeout=20.0) if owns_http else None

    try:
        while limit is None or result.attempted < limit:
            remaining = chunk_size if limit is None else min(chunk_size, limit - result.attempted)
            if remaining <= 0:
                break

            # The session is opened twice on purpose, and closed for the slow
            # part in between. Holding one open across the image fetches and
            # the GPU pass means holding row locks for minutes, which is what
            # deadlocked against a concurrent ingest_all on 2026-08-07: both
            # jobs write the same `listing` rows, and the wider the window the
            # likelier they interleave badly. Read ids, release, embed, write.
            with Session(db_engine) as session:
                listings = session.exec(
                    select(Listing)
                    .where(col(Listing.embedded_at).is_(None))
                    .order_by(col(Listing.id).asc())
                    .limit(remaining)
                ).all()

                if not listings:
                    break

                # Position-preserving: embed_image_urls returns one entry per
                # input in input order, so this zip is only safe because a
                # failed fetch comes back as None rather than being dropped.
                primary_images = [listing.images[0] if listing.images else None for listing in listings]
                listing_ids = [listing.id for listing in listings]

            vectors = (
                embedder(primary_images, http_client=http_client)
                if http_client is not None
                else embedder(primary_images)
            )

            updates = list(zip(listing_ids, vectors, strict=True))
            result.embedded += _write_embeddings(db_engine, updates)
            result.attempted += len(updates)

            logger.info(
                "embedded %d/%d listings so far", result.embedded, result.attempted
            )
    finally:
        if http_client is not None:
            http_client.close()

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = datetime.now(UTC)
    outcome = embed_pending()
    elapsed = (datetime.now(UTC) - started).total_seconds()
    print(
        f"Attempted {outcome.attempted} listings in {elapsed:.0f}s: "
        f"{outcome.embedded} embedded, {outcome.failed} had no usable image"
    )

from sqlmodel import Session, select

from api.models import EMBEDDING_DIM, Listing
from ml.embed_listings import embed_pending


def make_listing(n: int, images: list[str] | None = None) -> Listing:
    return Listing(
        source="ebay",
        source_id=f"v1|{n}|0",
        title=f"Listing {n}",
        price=100.0 + n,
        url=f"https://www.ebay.com/itm/{n}",
        images=["https://i.ebayimg.com/images/g/x/s-l225.jpg"] if images is None else images,
    )


def seed(test_engine, count: int, images: list[str] | None = None) -> None:
    with Session(test_engine) as session:
        for n in range(count):
            session.add(make_listing(n, images))
        session.commit()


def fake_embedder(urls, http_client=None):
    """One vector per input, None where there was no URL. Mirrors the real
    embed_image_urls contract, which is what embed_pending relies on."""
    return [None if url is None else [0.1] * EMBEDDING_DIM for url in urls]


def test_embed_pending_embeds_and_stamps(test_engine):
    seed(test_engine, 3)

    result = embed_pending(db_engine=test_engine, embedder=fake_embedder)

    assert (result.attempted, result.embedded, result.failed) == (3, 3, 0)
    with Session(test_engine) as session:
        for listing in session.exec(select(Listing)).all():
            assert listing.embedding is not None
            assert len(listing.embedding) == EMBEDDING_DIM
            assert listing.embedded_at is not None


def test_a_listing_with_no_images_is_stamped_so_it_is_never_retried(test_engine):
    """The reason the work queue keys on embedded_at rather than on
    `embedding IS NULL`. Twelve listings in the real corpus have no image, and
    keying on the vector would hand them back on every single run, forever."""
    seed(test_engine, 1, images=[])

    result = embed_pending(db_engine=test_engine, embedder=fake_embedder)

    assert (result.attempted, result.embedded, result.failed) == (1, 0, 1)
    with Session(test_engine) as session:
        listing = session.exec(select(Listing)).one()
        assert listing.embedding is None, "nothing to embed"
        assert listing.embedded_at is not None, "but it has been asked, and must not be re-asked"


def test_a_second_run_does_no_work(test_engine):
    """Idempotent: the whole point of stamping."""
    seed(test_engine, 3)
    embed_pending(db_engine=test_engine, embedder=fake_embedder)

    calls: list[int] = []

    def counting_embedder(urls, http_client=None):
        calls.append(len(urls))
        return fake_embedder(urls)

    result = embed_pending(db_engine=test_engine, embedder=counting_embedder)

    assert result.attempted == 0
    assert calls == [], "the embedder should not be invoked at all"


def test_limit_bounds_a_run(test_engine):
    """A scheduled job is capped so it stays restartable rather than owning
    the worker for hours."""
    seed(test_engine, 5)

    result = embed_pending(limit=2, db_engine=test_engine, embedder=fake_embedder)

    assert result.attempted == 2
    with Session(test_engine) as session:
        pending = session.exec(select(Listing).where(Listing.embedded_at == None)).all()  # noqa: E711
        assert len(pending) == 3


def test_a_failed_fetch_does_not_raise_and_leaves_the_rest_embedded(test_engine):
    seed(test_engine, 3)

    def failing_middle(urls, http_client=None):
        return [[0.1] * EMBEDDING_DIM, None, [0.1] * EMBEDDING_DIM]

    result = embed_pending(db_engine=test_engine, embedder=failing_middle)

    assert (result.attempted, result.embedded, result.failed) == (3, 2, 1)
    with Session(test_engine) as session:
        listings = session.exec(select(Listing).order_by(Listing.id)).all()
        assert [listing.embedding is not None for listing in listings] == [True, False, True]
        assert all(listing.embedded_at is not None for listing in listings), (
            "every attempt is stamped, including the failure"
        )


def test_chunking_still_processes_everything(test_engine):
    """Chunked paging is what makes a run resume-safe. It must not silently
    stop after the first chunk."""
    seed(test_engine, 5)
    chunks: list[int] = []

    def counting_embedder(urls, http_client=None):
        chunks.append(len(urls))
        return fake_embedder(urls)

    result = embed_pending(db_engine=test_engine, embedder=counting_embedder, chunk_size=2)

    assert chunks == [2, 2, 1]
    assert result.attempted == 5


def test_limit_smaller_than_chunk_size_is_respected(test_engine):
    """The chunk loop must not overshoot the limit by rounding up to a full
    chunk, which would make a 'bounded' job unbounded in practice."""
    seed(test_engine, 10)
    chunks: list[int] = []

    def counting_embedder(urls, http_client=None):
        chunks.append(len(urls))
        return fake_embedder(urls)

    result = embed_pending(
        limit=3, db_engine=test_engine, embedder=counting_embedder, chunk_size=500
    )

    assert chunks == [3]
    assert result.attempted == 3

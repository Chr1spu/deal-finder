"""The embedding write must survive a Postgres deadlock.

`embed_pending` and `ingest_all` both write the same `listing` rows, and on
2026-08-07 a real run lost a 480-row chunk to `deadlock detected`. Postgres
aborts one side of a deadlock rather than blocking, so this is a normal outcome
under concurrency and retrying is the standard remedy, not a workaround.

The expensive part (fetching images, running CLIP) must NOT be repeated on a
retry, which is why the retry lives around the write rather than the chunk.
"""

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from api.models import Listing
from ml import embed_listings
from ml.embed_listings import embed_pending


class FakeDeadlock(OperationalError):
    """OperationalError carrying a sqlstate, the way psycopg reports one."""

    def __init__(self, sqlstate="40P01"):
        super().__init__("UPDATE listing", {}, Exception("deadlock detected"))
        self.orig = type("Orig", (), {"sqlstate": sqlstate})()


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(embed_listings.time, "sleep", lambda _seconds: None)


def seed(engine, count):
    with Session(engine) as session:
        for n in range(count):
            session.add(
                Listing(
                    source="ebay",
                    source_id="deadlock-%d" % n,
                    title="RTX 4090 %d" % n,
                    price=100.0 + n,
                    url="https://example.com/%d" % n,
                    images=["https://i.ebayimg.com/images/g/abc/s-l225.jpg"],
                )
            )
        session.commit()


def constant_embedder(urls, **_kwargs):
    return [[0.1] * 512 for _ in urls]


def test_a_deadlock_is_retried_and_the_chunk_still_lands(test_engine, monkeypatch):
    seed(test_engine, 3)
    commits = {"n": 0}
    original = Session

    class OneDeadlockSession(original):
        """Fails the first commit only. The read session never commits, so
        this lands on the write, which is what the retry wraps."""

        def commit(self):
            commits["n"] += 1
            if commits["n"] == 1:
                raise FakeDeadlock()
            return super().commit()

    monkeypatch.setattr(embed_listings, "Session", OneDeadlockSession)

    result = embed_pending(db_engine=test_engine, embedder=constant_embedder)

    assert commits["n"] >= 2, "the write was retried after the deadlock"
    assert result.embedded == 3, "and every row still landed"


def test_the_embedder_is_not_re_run_when_the_write_is_retried(test_engine, monkeypatch):
    """The whole reason the retry wraps the write and not the chunk: images and
    GPU time are the expensive part and are already in hand."""
    seed(test_engine, 2)
    embed_calls = {"n": 0}

    def counting_embedder(urls, **_kwargs):
        embed_calls["n"] += 1
        return [[0.2] * 512 for _ in urls]

    commits = {"n": 0}
    original = Session

    class OneDeadlockSession(original):
        def commit(self):
            commits["n"] += 1
            if commits["n"] == 1:
                raise FakeDeadlock()
            return super().commit()

    monkeypatch.setattr(embed_listings, "Session", OneDeadlockSession)
    embed_pending(db_engine=test_engine, embedder=counting_embedder)

    assert embed_calls["n"] == 1, "embeddings computed once despite the retry"


def test_a_non_retryable_error_is_raised_immediately(test_engine, monkeypatch):
    """A deadlock is expected under concurrency; a constraint violation is a
    bug, and swallowing it would hide exactly the class of failure that took
    intake down for twelve hours."""
    seed(test_engine, 1)
    original = Session

    class AlwaysFailing(original):
        def commit(self):
            raise FakeDeadlock(sqlstate="23502")  # not_null_violation

    monkeypatch.setattr(embed_listings, "Session", AlwaysFailing)

    with pytest.raises(OperationalError):
        embed_pending(db_engine=test_engine, embedder=constant_embedder)


def test_rows_are_left_unstamped_when_the_write_ultimately_fails(test_engine, monkeypatch):
    """So the next run retries them rather than marking them done."""
    seed(test_engine, 2)
    original = Session

    class AlwaysDeadlocking(original):
        def commit(self):
            raise FakeDeadlock()

    monkeypatch.setattr(embed_listings, "Session", AlwaysDeadlocking)

    with pytest.raises(OperationalError):
        embed_pending(db_engine=test_engine, embedder=constant_embedder)

    with Session(test_engine) as session:
        pending = session.exec(select(Listing).where(Listing.embedded_at.is_(None))).all()
    assert len(pending) == 2, "still pending, so the next run picks them up"

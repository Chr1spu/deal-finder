import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from api.settings import settings


@pytest.fixture()
def test_engine():
    """A throwaway in-memory SQLite DB, schema created fresh per test.

    SQLite stands in for Postgres here, good enough for exercising our own
    upsert/status-flip logic without needing a real DB running. Anything that
    depends on Postgres-only behavior (pgvector, native JSON operators)
    should NOT be tested against this fixture.
    """
    # StaticPool matters more than it looks. An in-memory SQLite database
    # belongs to its *connection*, and the default pool hands out a new
    # connection per thread, so a test that crosses threads (anything driving
    # the app through fastapi's TestClient) would otherwise create the schema
    # on one connection and query an empty database on another. StaticPool
    # keeps one connection for the whole engine, which is what "a throwaway
    # database for this test" was always meant to mean.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)


@pytest.fixture()
def test_session(test_engine):
    with Session(test_engine) as session:
        yield session


@pytest.fixture(scope="session")
def pg_engine():
    """A real Postgres engine, for the things SQLite cannot stand in for.

    SQLite tolerates a pgvector `Vector` column (it accepts arbitrary type
    names, and the DDL goes through the generic type compiler), so schema
    creation and round-tripping work fine on the default fixture. What it
    cannot do is *behave* like pgvector: the distance operators (`<=>`), the
    real column type, and index support are Postgres-only. Those need this.

    Skips rather than fails when the database is unreachable, deliberately,
    so `uv run pytest` stays one green command on a machine with no container
    running. A test that silently doesn't run is a real cost, so the skip
    message says exactly how to make it run.

    Session-scoped because creating the schema per test is slow and nothing
    here mutates it; individual tests clean up their own rows.
    """
    engine = create_engine(settings.test_database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            connection.commit()
    except Exception as exc:  # pragma: no cover - depends on local environment
        pytest.skip(
            f"Postgres not reachable at {settings.test_database_url} ({type(exc).__name__}). "
            "Create it with: docker exec infra-postgres-1 createdb -U dealfinder dealfinder_test"
        )

    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def pg_session(pg_engine):
    """Rolls back whatever a test wrote, so the session-scoped schema above
    can be shared without tests leaking rows into each other."""
    with Session(pg_engine) as session:
        yield session
        session.rollback()


TEST_API_KEY = "test-key-not-a-real-secret"


@pytest.fixture()
def api_key(monkeypatch):
    """Configure an API key for tests that exercise write endpoints.

    Writes fail CLOSED when no key is set (docs/decisions/0017), so without
    this fixture every POST/PATCH/DELETE returns 503. That is the intended
    production behaviour, and tests/test_auth.py asserts it deliberately.
    """
    monkeypatch.setattr(settings, "api_key", TEST_API_KEY)
    return TEST_API_KEY


@pytest.fixture()
def auth_headers(api_key):
    return {"X-API-Key": api_key}

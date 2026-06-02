import pytest
from sqlmodel import Session, SQLModel, create_engine


@pytest.fixture()
def test_engine():
    """A throwaway in-memory SQLite DB, schema created fresh per test.

    SQLite stands in for Postgres here, good enough for exercising our own
    upsert/status-flip logic without needing a real DB running. Anything that
    depends on Postgres-only behavior (pgvector, native JSON operators)
    should NOT be tested against this fixture.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)


@pytest.fixture()
def test_session(test_engine):
    with Session(test_engine) as session:
        yield session

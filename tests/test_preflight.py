"""Schema-drift detection.

This guards the specific silent failure that hit this project three times, and
worst on 2026-08-01: migrations added NOT NULL columns while long-lived RQ
workers held an older import of the models. Every insert failed, `ingest_all`
swallowed each one inside its per-search error isolation, the job reported
success, the failed-job registry stayed empty, and ingestion was dead for
hours while looking healthy.
"""

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table
from sqlmodel import Field, SQLModel

from api.models import Listing, SavedSearch
from systems.preflight import SchemaDriftError, assert_schema_current


def test_a_current_schema_passes(pg_engine):
    """The real models against the real database: the ordinary case."""
    assert_schema_current(pg_engine, Listing, SavedSearch)


def test_a_required_column_the_model_does_not_know_about_is_caught(pg_engine):
    """The failure mode itself. A model missing a NOT NULL column means every
    insert will fail with a NotNullViolation, one row at a time, inside an
    exception handler that exists for a different reason."""

    class Stale(SQLModel, table=True):
        __tablename__ = "preflight_drift_probe"
        id: int | None = Field(default=None, primary_key=True)
        known: str = "x"

    metadata = MetaData()
    Table(
        "preflight_drift_probe",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("known", String, nullable=False),
        # Added by a migration the running code predates.
        Column("added_later", String, nullable=False),
    )
    metadata.create_all(pg_engine)
    try:
        with pytest.raises(SchemaDriftError) as caught:
            assert_schema_current(pg_engine, Stale)
        message = str(caught.value)
        assert "preflight_drift_probe.added_later" in message
        assert "Restart the workers" in message, "must say what to actually do"
    finally:
        metadata.drop_all(pg_engine)
        SQLModel.metadata.remove(Stale.__table__)


def test_a_nullable_extra_column_is_not_drift(pg_engine):
    """Only required columns matter. A nullable one the code does not set is
    harmless, and flagging it would make the check cry wolf on every additive
    migration."""

    class Partial(SQLModel, table=True):
        __tablename__ = "preflight_nullable_probe"
        id: int | None = Field(default=None, primary_key=True)

    metadata = MetaData()
    Table(
        "preflight_nullable_probe",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("optional", String, nullable=True),
    )
    metadata.create_all(pg_engine)
    try:
        assert_schema_current(pg_engine, Partial)
    finally:
        metadata.drop_all(pg_engine)
        SQLModel.metadata.remove(Partial.__table__)


def test_a_missing_table_is_not_drift(pg_engine):
    """A model whose table does not exist yet is the ordinary "migration not
    applied" case, which Alembic reports clearly and which fails loudly on
    first use. Only the reverse direction is dangerous."""

    class NotCreated(SQLModel, table=True):
        __tablename__ = "preflight_absent_probe"
        id: int | None = Field(default=None, primary_key=True)

    try:
        assert_schema_current(pg_engine, NotCreated)
    finally:
        SQLModel.metadata.remove(NotCreated.__table__)

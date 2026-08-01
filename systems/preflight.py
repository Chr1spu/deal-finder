"""Fail fast when a worker's code is older than the database it writes to.

This exists because of a specific, silent failure that has now happened three
times in this project's short life, and was worst the last time:

  Migrations 0013-0015 added NOT NULL columns (`is_accessory`, `price_is_from`,
  `enabled`). The running RQ workers had imported `api.models.Listing` before
  those fields existed, so every INSERT omitted them and Postgres rejected it
  with a NotNullViolation. Ingestion was dead for hours.

  It looked completely healthy. `ingest_all` isolates errors per search
  (deliberately, so one bad search cannot kill 64), so each failure was logged
  and swallowed, the job reported "Successfully completed", the failed-job
  registry stayed empty, and the worker's `failed_job_count` stayed at zero.
  The only visible symptom was that new listings stopped appearing, which
  looks identical to a quiet day on eBay.

A long-lived worker holds whatever it imported at startup. Migrations move the
database forward underneath it. Nothing in RQ, SQLModel or Alembic notices,
because from each component's point of view nothing is wrong.

So the jobs check for themselves, once, at the top of a run. One query, and it
turns a silent multi-hour outage into an immediate loud failure that names the
missing columns and says to restart the worker.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect
from sqlmodel import SQLModel

from api.db import engine as default_engine

logger = logging.getLogger(__name__)


class SchemaDriftError(RuntimeError):
    """The database has required columns this process's models do not know
    about, which means this process is running stale code."""


def assert_schema_current(db_engine: Engine | None = None, *tables: type[SQLModel]) -> None:
    """Raise if the DB has NOT NULL columns the in-memory models lack.

    Only that direction is checked, deliberately. A model knowing about a
    column the database lacks is the ordinary "migration not applied yet"
    case, which Alembic already reports clearly and which fails loudly on
    first use. The dangerous direction is the reverse: the database requiring
    something the code never sends, which fails per-row inside an exception
    handler that exists for a different reason.
    """
    db_engine = db_engine or default_engine
    inspector = inspect(db_engine)
    existing_tables = set(inspector.get_table_names())

    problems: list[str] = []
    for model in tables:
        # SQLModel sets both of these on table classes, but types them on the
        # metaclass rather than the class, so neither is visible to mypy.
        table_name = str(model.__tablename__)
        table = model.__table__  # type: ignore[attr-defined]
        if table_name not in existing_tables:
            continue
        known = {c.name for c in table.columns}
        for column in inspector.get_columns(table_name):
            required = not column["nullable"] and column.get("default") is None
            if required and column["name"] not in known:
                problems.append(f"{table_name}.{column['name']}")

    if problems:
        raise SchemaDriftError(
            "This process is running code older than the database. It does not know about "
            f"required column(s): {', '.join(sorted(problems))}. Every insert will fail with "
            "a NotNullViolation, and because jobs isolate errors per item that failure is "
            "easy to miss. Restart the workers and scheduler from the current tree."
        )

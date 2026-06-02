# Deal Finder

A secondhand-marketplace deal finder. Ingests listings (eBay first), extracts image and text features, compares against a self-built history of comparable sold items, and scores how good a deal is.

Full plan: [PROJECT_PLAN.md](PROJECT_PLAN.md).

**Status:** Stage 1 (ingestion + data model) in progress.

## Setup

1. Copy `.env.example` to `.env` and fill in eBay Browse API credentials (developer.ebay.com, sandbox keys are enough to start).
2. Install [uv](https://docs.astral.sh/uv/), then `uv sync`. This creates `.venv` and installs pinned dependencies (see `pyproject.toml` / `uv.lock`).
3. `docker compose -f infra/docker-compose.yml up -d` to start Postgres (pgvector) and Redis.
4. `uv run alembic upgrade head` to create the `listing` table.
5. `uv run python -m connectors.ingest_ebay` to pull one hardcoded search query into the DB.
6. `uv run uvicorn api.main:app --reload`, then `GET http://localhost:8000/listings`.
7. `uv run pytest` to run the test suite (no live services needed, DB tests use in-memory SQLite and eBay calls are mocked).

## Architecture overview

To be filled in as the pipeline layers land. See PROJECT_PLAN.md for the target shape.

## Notable engineering decisions

See [docs/decisions/](docs/decisions/).

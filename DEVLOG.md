# Devlog

## 2026-06-02 - Self-review: DB tests, uv migration, embedding schema decision

**Did:**
- Added `tests/conftest.py` (in-memory SQLite fixture) and tests for `ingest_ebay.py`'s upsert logic and `disappearance_check.py`'s status-flipping logic. Both were previously untested despite being the two DB-touching connector scripts.
- Refactored both to take injectable `client`/`db_engine` params so tests don't need real eBay/Postgres.
- Replaced `requirements.txt` with `pyproject.toml` + `uv` (dependencies, Ruff, mypy, and pytest config now all live in one file).
- Created `LEARNING_LOG.md`, a standing reference doc (repo map, tool glossary, decision log), and added a note to `CLAUDE.md` to keep it updated every session, including whenever a "locked-in" choice gets swapped for a better one.

**Decided:**
- One embedding per listing, not per image, for the Stage 3 CLIP work. See `LEARNING_LOG.md`'s decision log for the full reasoning. Not implemented yet, just settled ahead of time.
- `CLAUDE.md` and `PROJECT_PLAN.md` choices are defaults, not commitments. They're changeable any time a better option turns up, as long as it's explained in `LEARNING_LOG.md`.

**Broke / debugged:**
- N/A.

**Next:**
- Install `uv` and generate `uv.lock`.
- Register an eBay Developer sandbox app, fill in `.env`, and run the full Stage 1 setup end to end with real data.

## 2026-06-02 - Repo scaffold + Stage 1 vertical slice

**Did:**
- Renamed planning docs (`Claude.md` to `CLAUDE.md`, `Project Plan.md` to `PROJECT_PLAN.md`) so the cross-reference between them actually resolves.
- Ran `git init`, laid out the repo skeleton (`connectors/`, `api/`, `tests/`, `infra/`, `docs/decisions/`).
- `infra/docker-compose.yml`: Postgres (pgvector image) and Redis only, matching stage 1 scope.
- `Listing` SQLModel (`api/models.py`) plus the first Alembic migration.
- eBay Browse API client (`connectors/ebay.py`), OAuth client-credentials flow, `search_items`, `get_item`.
- Normalizer (`connectors/normalizer.py`) mapping raw eBay item summaries into `Listing`.
- `connectors/ingest_ebay.py`: one hardcoded search query, normalized, then upserted.
- `connectors/disappearance_check.py`: re-checks active listings and marks vanished ones `likely_sold`. Not wired to a scheduler yet (that's stage 2); run it manually or via cron for now so sold-price data starts accumulating early.
- `GET /listings` route plus `GET /health`.
- Fixture-based tests for the normalizer and the eBay client, respx-mocked HTTP, no live API calls.

**Decided:**
- Disappearance checking runs as a plain script for now instead of waiting on the stage-2 RQ scheduler, since the plan calls for starting it as early as possible so it has time to accumulate data.
- Repo lives directly in this folder rather than in a nested `deal-finder/` subdirectory.

**Broke / debugged:**
- N/A, first commit.

**Next:**
- Register an eBay Developer sandbox app, fill in `.env`, run `alembic upgrade head` and `ingest_ebay.py` against real data.
- Verify `GET /listings` returns real rows before moving to stage 2 (Redis queue and scheduler, rate limiting, dedup).

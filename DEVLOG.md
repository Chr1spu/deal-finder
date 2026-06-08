# Devlog

## 2026-06-08 - Closing out stage 1: saved-search config

**Did:**
- Added a `SavedSearch` model (`keyword`, `location`, `created_at`) to `api/models.py` and a migration (`0002_create_saved_search.py`) that creates the table and seeds one default row ("nintendo switch").
- Refactored `connectors/ingest_ebay.py`: replaced the single hardcoded `SEARCH_QUERY` constant with `ingest_saved_search()` (runs one saved search) and `ingest_all()` (loops over every row in the `savedsearch` table). `python -m connectors.ingest_ebay` now runs `ingest_all()`.
- Rewrote `tests/test_ingest_ebay.py` for the new interface, including a test that running two saved searches which happen to return the same eBay item still upserts a single row instead of two.
- Added real checkboxes to `PROJECT_PLAN.md`'s roadmap (they were referenced in `CLAUDE.md`'s "Current phase" note but never actually existed) and checked off everything now done in stage 1.

**Decided:**
- `SavedSearch.location` is stored but not yet passed to eBay's API. eBay's Browse API only supports country-level delivery/pickup filters, not the free-text proximity search this field implies. Documented as a known gap rather than faking a filter that wouldn't really work, revisit once a genuinely local source (Facebook Marketplace) needs it.
- Saved searches are seeded via migration data for now, not a CRUD interface. Full CRUD is explicitly stage 5 scope; stage 1 only needed the config to exist as data, per "no UI yet."

**Broke / debugged:**
- N/A.

**Next:**
- Run stage 1 for real on a machine with Docker and Python installed: `docker compose up`, `alembic upgrade head`, `python -m connectors.ingest_ebay` against live eBay data, confirm `GET /listings` returns real rows. Stage 1 is code-complete but not yet verified end to end, so stage 2 shouldn't start until that happens.
- Install `uv` and generate `uv.lock` (carried over from last session).

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

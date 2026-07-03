# Devlog

## 2026-07-03 - Going to real production data, at real scale

**Did:**
- Switched `.env` from eBay sandbox to production (real Client ID/Secret), kept the old sandbox keyset commented out for reference.
- Added a default condition filter to `EbayClient.search_items()` (`exclude_new=True`), excluding condition IDs 1000/1500/1750 (New, New other, New with defects) and keeping refurbished/used/for-parts. Verified the exact filter syntax against the real production API before committing to it, rather than trusting docs/memory. This is a secondhand deal finder, brand-new listings have no resale depreciation to find a deal in.
- Raised `search_items()`'s default `limit` from 50 to 200, eBay Browse API's actual per-call max. Verified live that eBay actually honors 200.
- Seeded 63 new saved searches: every current Nintendo Switch variant (Lite, OLED, 2), iPhone 15 and newer (through the 17 line, plus 16e/Air), and a representative spread of recent CPUs, GPUs, RAM, and storage. 64 total now, up from 1.
- Bumped RQ's per-job timeout to 1 hour for both the ingest and disappearance-check jobs (`systems/queue.py`), since the default 180s would've killed a real run of this size partway through. Found this before it became a real problem, not after.
- Ran the first full ingest across all 64 searches for real: 11,263 upserts processed, 10,073 unique listings landed (the gap is overlap between different searches matching the same real item, expected), 99.9% got a real `image_hash`. Took about 30-35 minutes for this first run (every listing was new, so every one triggered an image download). Table size after: 9.16 MB for 10,073 rows, ~930 bytes/row at real scale, well below the earlier small-sample estimate.
- Added a "Post-completion backlog" section to `PROJECT_PLAN.md`: parallelizing ingestion (currently fully sequential, a real opportunity now that there's real volume), pagination past the 200-result cap, capturing shipping cost (flagged as more than a nice-to-have, a real correctness gap for stage 4's scoring), rate limiting at larger scale, and eventual data retention.

**Decided:**
- Condition filter excludes New/New-other/New-with-defects only, keeps all refurbished and used grades plus for-parts, since those are exactly where secondhand deals actually exist.
- 200 as the per-search cap, not more: it's eBay's actual maximum per call, going beyond it needs real pagination, deferred to the backlog above rather than solved today.
- Job timeouts bumped to a round, generous 1 hour rather than tuned precisely, since the actual runtime at this scale wasn't known ahead of time.

**Broke / debugged:**
- N/A this session, though the job-timeout issue above was caught proactively (reasoned about job duration at the new scale before running it for real) rather than discovered by a job dying mid-run.

**Next:**
- Scheduler + worker aren't running continuously yet, on request, until the user is ready. Once turned on: hourly ingestion, disappearance-checking every 6 hours, both against all 64 searches.
- The "Post-completion backlog" items are explicitly deferred, not needed to keep using the system as-is.

## 2026-06-26 - Stage 2: systems layer built and verified end to end

**Did:**
- Built `systems/ratelimit.py` (retry-with-backoff for 429/5xx/transport errors) and wrapped all three of `connectors/ebay.py`'s HTTP calls with it.
- Built `systems/queue.py` (RQ queue bound to Redis, `enqueue_ingest_all`/`enqueue_disappearance_check`) and `systems/scheduler.py` (a sleep-loop that enqueues both on independent, configurable intervals).
- Generalized `connectors/disappearance_check.py` from eBay-only to a `PULL_BASED_SOURCES` registry plus `check_all_sources()`, per the ADR from the last session.
- Added the compute-only half of image-hash dedup: `image_hash` column + migration, `connectors/image_hash.py` (perceptual hash via `imagehash`/Pillow), wired into `ingest_ebay.py` so new (or previously unhashed) listings get their primary photo hashed. Wrote `docs/decisions/0002-image-hash-dedup.md` first, since it's a new dependency plus a schema change.
- Added 26 new/updated tests (ratelimit, queue, scheduler, generalized disappearance-check, image hashing, ingest). Full suite: 30 passed.
- Fixed a pre-existing broken test (`test_search_items_without_credentials_raises_clear_error`): it passed `client_id=""` expecting that to simulate "no credentials," but that's falsy, so `EbayClient`'s `or` fallback was silently picking up the real sandbox credentials `.env` has had since the last session. Not something this session's changes caused, just never caught since no one re-ran the full suite after those credentials were added.
- Verified everything for real against the actual Docker Postgres/Redis: ran the migration, ran a real ingest (sandbox item has no images, so nothing to hash there, but confirmed the real network+Pillow+imagehash path against a live public image URL separately), enqueued both jobs against real Redis and ran an `rq worker` to process them.

**Decided:**
- Scheduler is a plain sleep-loop, not `rq-scheduler`, keeping the same "RQ not Celery" minimal-moving-parts reasoning.
- Image-hash dedup is compute-and-store only this stage. The actual cross-source duplicate-matching logic is deferred until Depop exists and there's a real second source to design the matching rules against.
- Perceptual hash (`imagehash.phash`) over an exact byte hash, since eBay/Depop each re-encode photos independently and an exact hash would basically never match cross-source.

**Broke / debugged:**
- Plain `rq worker` doesn't run at all on Windows: RQ's default `Worker` calls `os.fork()`, which doesn't exist on this platform. Its `SimpleWorker` (no fork) gets further but then fails enforcing job timeouts via `signal.SIGALRM`, also missing on Windows. Fixed with `systems/queue.py::WindowsWorker` (`SimpleWorker` + `TimerDeathPenalty`, which uses `threading.Timer` instead), confirmed working by actually running both queued jobs to completion. Expected to be a non-issue once this runs in a Linux container (stage 7).

**Next:**
- Stage 2 is done and verified. Stage 3 (feature pipeline: CLIP embeddings, NLP extraction) is next per the build order, once `connectors/depop.py` (which stage 2 was explicitly meant to unblock) is decided on, or straight into stage 3 if Depop stays deferred a while longer.

## 2026-06-21 - Stage 1 fully verified: real eBay sandbox data end to end

**Did:**
- Registered eBay sandbox credentials, filled `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` into `.env`.
- Brought Postgres/Redis back up (`docker compose up -d`), confirmed both healthy and already at migration head (the named volume survived the container recreate).
- Ran `python -m connectors.ingest_ebay` for real against eBay's sandbox API: upserted 1 listing (a Joy-Con set matching the seeded "nintendo switch" saved search).
- Confirmed `GET /listings` returns that real row and `GET /health` returns ok.

**Decided:**
- N/A, this was verification, not new design.

**Broke / debugged:**
- Port 8000 was held by a stale `uvicorn` process left running from an earlier session, which made the first `/listings` check return a 500. Not a bug in this session's code, just an old server still holding the port. Killed it and started a clean one.

**Next:**
- Stage 1 is done. Stage 2 (Redis job queue + scheduler, rate limiting/backoff, dedup) is next per the build order, before Depop or the Facebook extension get built (see `docs/decisions/0001-multi-source-connector-strategy.md`).
- Consider switching `.env`'s `EBAY_ENV` from `sandbox` to `production` once ready to pull real, non-test eBay data.

## 2026-06-21 - Multi-source connector strategy: Depop and Facebook Marketplace

**Did:**
- Wrote `docs/decisions/0001-multi-source-connector-strategy.md`, the first real ADR, deciding how Depop and Facebook Marketplace actually get ingested given neither has an official API and Facebook's ToS additionally prohibits scraping.
- Updated `PROJECT_PLAN.md` (practical notes, repo structure) and `CLAUDE.md` (constraints, repo structure) to match.
- Added Session 5 / Step 22 to `LEARNING_LOG.md` explaining the decision in build-guide detail.

**Decided:**
- Depop stays pull-based like eBay: a scheduled connector, following the same client/normalizer/ingest pattern as `connectors/ebay.py`, hitting Depop's unofficial endpoints. Low-investment, expect breakage, same framing as before, now made concrete as "pull-based."
- Facebook Marketplace goes push-based instead: a browser extension running in the user's own logged-in session captures one listing at a time on click and posts it to the API. No server-side Facebook connector, and no automated disappearance-tracking for Facebook-sourced listings, only manual re-capture. Full reasoning and alternatives considered are in the ADR.
- OfferUp is deferred, not addressed this session.

**Broke / debugged:**
- N/A.

**Next:**
- Finish verifying stage 1 end to end (the live eBay API call is still the one open item).
- Once stage 1 is verified and stage 2's systems layer (queue, scheduler, rate limiting, dedup, and generalizing `disappearance_check.py` to loop per source) is in place, implement `connectors/depop.py` first, since it reuses the eBay pattern almost directly, then the Facebook browser extension.

## 2026-06-13 - Real verification: Docker, uv, migrations, tests, live API

**Did:**
- Installed WSL2, Docker Desktop, and `uv` on this machine (none were present before). WSL2 needed an elevated terminal and a restart, so this was a stop-and-resume across two turns.
- Brought up Postgres (pgvector) and Redis for real via `docker compose up -d`, both report healthy.
- Ran `uv sync`: installed Python 3.12.14 (uv manages its own Python installs) and all 42 dependencies, generating `uv.lock` for the first time.
- Ran both Alembic migrations against the real database. Confirmed the `savedsearch` table has its seeded "nintendo switch" row.
- Ran the full test suite for real for the first time: all 13 tests passed on the first try.
- Fixed a Pydantic deprecation warning in `api/settings.py` (`class Config` to `model_config = SettingsConfigDict(...)`, the Pydantic v2-native way of doing the same thing).
- Started the API with `uvicorn` and confirmed `GET /health` and `GET /listings` respond correctly against the real database (`/listings` correctly returns `[]`, since no eBay ingestion has run yet).

**Decided:**
- N/A, this session was verification, not new design decisions.

**Broke / debugged:**
- `wsl --install` silently failed with a confusing "not installed" message when run from this non-elevated shell instead of a clear permissions error. Root cause: WSL2 setup needs administrator rights, which this session's shell access doesn't have. Fixed by having the user run it themselves from an elevated PowerShell.

**Next:**
- Register an eBay Developer sandbox app, fill in `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` in `.env`, run `python -m connectors.ingest_ebay` for real, and confirm `GET /listings` returns live eBay data. That's the one remaining piece before stage 1 counts as fully done end to end.
- Commit `uv.lock` now that it actually exists.

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

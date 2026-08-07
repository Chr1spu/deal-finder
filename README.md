# Undercut

A secondhand-marketplace deal finder. Ingests listings, extracts image and text features, compares against a self-built history of comparable sold items, and scores how good a deal is.

**eBay is the price oracle and the only comp source.** It is ingested, disappearance-tracked, and used to build the price history everything else is measured against. Depop and Facebook Marketplace are valuation *clients*: their listings get identified and scored against eBay-derived value, and contribute no comps of their own, because item variety there is too high for per-item history built from them to mean anything. That makes the central technical problem cross-source item identification, since a foreign listing carries no eBay catalog id. See [docs/decisions/0008](docs/decisions/0008-price-oracle-and-valuation-clients.md).

Full plan: [PROJECT_PLAN.md](PROJECT_PLAN.md).

**Status:** Stages 1-4 complete and running. Sold-price history is accumulating from disappearance tracking, listings are embedded and attribute-extracted, and the valuation engine produces deal scores. Stage 5 is done apart from the frontend: `GET /deals` serves a ranked feed, saved searches are managed over the API, writes are authenticated, and new deals post to a Discord webhook. Next is the React dashboard (stage 6) and deployment (stage 7).

### Capturing a listing from Depop or Facebook

Neither can be polled server-side: Facebook's ToS forbids it, and Depop returns 403 to every server-side request behind Cloudflare Bot Management. So a browser extension reads the page you are already looking at, when you click the button, and values it against eBay prices.

1. Start the API (`uv run uvicorn api.main:app`) and the workers.
2. Load `extension/` as an unpacked extension (`chrome://extensions` with Developer Mode on).
3. Paste your `API_KEY` into the extension popup's key field (stored per-install, never committed).
4. Open a Depop or Facebook Marketplace listing and click the toolbar button.

The overlay shows what comparable eBay listings are **asking**, alongside the candidates it matched. It deliberately does not compute a "% below market" figure: those are asking prices, not sold prices, and the number would imply a precision the data does not yet have.

**On a first live run, check these four things in order.** This path has been exercised end to end in tests but not yet against a real page, so a failure here is expected to be a page-parsing problem rather than a pipeline one.

1. The button is enabled. If the popup says to open a listing page, the URL is not one the manifest injects into (`www.depop.com/products/*`, `www.facebook.com/marketplace/item/*`); the popup reads that list from the manifest, so those are the only two.
2. The capture is stored: the overlay names the listing and its price. A "could not read this listing" message means the page markup moved, which is expected occasionally and is a parser fix in `extension/sites/`.
3. `analysed: false` on the first click is normal. The API has no torch, so embedding is queued for the ML worker; click "Check again" a moment later.
4. Candidates come back non-empty. An empty list on a listing that plainly resembles eBay stock is the signal worth investigating: it means retrieval is being filtered to nothing rather than finding nothing (this is what `category=brand` used to do to every branded capture, see ADR [0018](docs/decisions/0018-memory-kits-are-one-product.md)'s sibling fix in the 2026-08-07 devlog entry).

## Setup

1. Copy `.env.example` to `.env` and fill in eBay Browse API credentials (developer.ebay.com).
   Also set `API_KEY` to any long random string (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
   **Leaving it empty refuses all writes rather than disabling the check**, so captures and
   saved-search changes will return 503 until it is set. That is deliberate: an unset secret
   must never mean an open endpoint. See [docs/decisions/0017](docs/decisions/0017-api-key-auth.md).
2. **Choose where the virtualenv lives, before installing anything.** If this repo sits in a cloud-synced folder (OneDrive, Dropbox), point uv somewhere else first, because the ML dependencies take the venv to roughly 3 GB and sync tools do not read `.gitignore`:
   ```
   setx UV_PROJECT_ENVIRONMENT C:\venvs\undercut     # Windows, persists for future shells
   export UV_PROJECT_ENVIRONMENT=~/.venvs/undercut   # macOS/Linux, add to your shell profile
   ```
   The repo itself does not move, so git is unaffected.
3. Install [uv](https://docs.astral.sh/uv/), then `uv sync` for the API, connectors and tests. Add `uv sync --group ml` only on the machine that runs embeddings (installs PyTorch with CUDA, see `pyproject.toml`).
4. `docker compose -f infra/docker-compose.yml up -d` to start Postgres (pgvector) and Redis.
5. `uv run alembic upgrade head` to create the schema.
6. `uv run python -m connectors.ingest_ebay` to run every saved search into the DB.
7. `uv run --group ml python -m ml.embed_listings` to embed listings that have no vector yet. Safe to re-run: it is idempotent and resumes where it stopped.
8. `uv run uvicorn api.main:app --reload`, then:
   - `GET /deals` the ranked deal feed (served from the last scheduled scan)
   - `GET /deals/{id}` value one listing on demand
   - `GET /saved-searches` the keywords in use, with the daily call budget they cost
   - `GET /listings` the raw corpus, paginated
9. `uv run pytest` to run the test suite. No live services required: DB tests use in-memory SQLite, eBay calls are mocked, and the few tests needing real Postgres or torch skip themselves when unavailable.

### The dashboard

```
cd frontend
npm install
npm run dev        # http://localhost:5173
```

Deal feed with the comps behind every score, saved-search management with the
live eBay call budget, and a price chart. Reads work without a key; changing
searches needs the `API_KEY` pasted into the field in the header.

The Vite dev server proxies `/api` to `127.0.0.1:8000`, so the browser makes
same-origin requests and CORS never enters the picture. It is `127.0.0.1` and
not `localhost` on purpose: Node resolves `localhost` to IPv6 first and uvicorn
binds IPv4, which otherwise produces a 500 from the proxy while the API answers
direct requests perfectly.

`package.json` overrides rollup to `@rollup/wasm-node`. That is not a
preference: on some Windows machines an Application Control policy refuses to
load rollup's unsigned native binary, and the error rollup prints blames npm's
optional-dependency bug rather than saying so. The WASM build is the same
rollup with no native module.

### Running the workers

Two queues, two workers, and they are not interchangeable. RQ hands a worker whatever job is next, so a single shared queue would eventually hand the deliberately torch-free ingest worker a GPU job and kill it on `import torch`.

```
rq worker undercut        --worker-class systems.queue.WindowsWorker   # ingest + disappearance check
rq worker undercut-ml     --worker-class systems.queue.WindowsWorker   # embeddings (needs --group ml)
uv run python -m systems.scheduler                                     # enqueues all three job types
```

Run **exactly one of each**. Duplicate or stale workers have twice caused silent failures here: a leftover worker keeps executing whatever code it imported at startup, so it can quietly run a months-old version of a job against a current database. Drop `--worker-class` once these run inside Linux containers; it exists because RQ's default worker forks, which Windows cannot do.

## Architecture overview

```
  PULL (eBay only, the price oracle)
  saved searches ──► connectors/ingest_ebay ──► normalizer ──► listing table
                                                                    │
         disappearance_check ◄────────────────────────────────────  │
         reads the response BODY, not a 404: eBay serves ended       │
         listings at 200 with OUT_OF_STOCK. Marks likely_sold,       │
         scores sale vs. price confidence, detects relists.          │
                                                                    ▼
                                              ml/embed_listings ──► embedding vector(512)
                                                                    │
  PUSH (Depop, Facebook: 403 / ToS, so never fetched server-side)    │
  browser extension ──► POST /capture ──► connectors/capture ────────┤
     (user clicks on a page they already have open)                  ▼
                                                              ml/match
                                                   1. image_hash exact match
                                                   2. CLIP k-NN over eBay
                                                          │
                                                          ▼
                                          candidates + what eBay is ASKING
                                                          │
  sold listings (likely_sold) ──────────► ml/valuation ◄──┘
     gated on sale_confidence,            estimate, confidence, deal score
     weighted by price_confidence         (no estimate below 3 comps)
                                                          │
                                                          ▼
                                    GET /deals  +  Discord alerts
```

**A note on what the deal score is not.** Comps are listings that *left the market*, which means sold, expired unsold, or withdrawn: eBay does not distinguish them. Estimates are therefore biased high and deal scores optimistic, which is why every response ships its comps and caveats rather than a bare percentage. See [docs/decisions/0014](docs/decisions/0014-valuation-and-deal-scoring.md).

Everything runs on Docker Compose (Postgres + pgvector, Redis) with RQ for job scheduling. The eBay connector lives inside a measured 5,000 calls/day budget; see [docs/decisions/0003](docs/decisions/0003-ebay-call-budget.md) for why that shapes so much of the design.

## Notable engineering decisions

See [docs/decisions/](docs/decisions/).

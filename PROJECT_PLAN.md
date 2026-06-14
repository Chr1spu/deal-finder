# Deal Finder

A secondhand-marketplace deal finder: ingests listings from eBay (and later Depop / Facebook Marketplace), extracts image and text features, compares each listing against a self-built history of comparable sold items, and scores how good a deal it is. Surfaced through a live dashboard with saved searches and alerts.

Goal: a resume/portfolio project that shows range (data engineering, systems, ML/CV, backend, full-stack, deployment) anchored by one coherent problem, with a clear path to more ambitious features once the core works.

---

## 1. Core Feature: Deal Scanner (build this first)

This is the heart of the project: scan listings across marketplaces, and for each one, figure out whether it's actually a good deal by comparing it to what similar items have recently sold for.

### What it does

1. Pull in listings matching a saved search (keywords + location)
2. Understand each listing (what it is, its condition, its category) from both the photos and the description
3. Find comparable recently-sold items
4. Score the deal (e.g. "34% below the median of 12 comparable sales")
5. Surface it to the user, live, with alerts for saved searches

### The layers

**Ingestion** Per-marketplace connectors (eBay via official API first) that normalize every source into one shared `Listing` schema: title, price, images, location, condition, category, posted date, source, source ID.

**Sold-history tracking** Most sources don't hand you sold-price data. Instead, periodically re-check tracked listings; if one disappears, mark it `likely_sold` and log it as a comp. This is how the price-history dataset gets built over time, so it should start running from week 1.

**Systems** Redis-backed job queue and scheduler for polling, per-source rate limiting, dedup (listing ID + image hash), retry/backoff, and a circuit-breaker for when a source starts blocking requests.

**ML / CV + NLP** CLIP embeddings on listing photos (category/condition signal, and the basis for similarity search). Lightweight NLP extraction of brand/model/size/condition from the description text.

**Valuation engine** Given a new listing's embedding and extracted features, run a k-NN search against comps in `pgvector`, then compute a deal score with a confidence weight based on comp count, recency, and similarity.

**Backend** FastAPI: auth, saved-search CRUD, deal feed endpoints, alert triggers.

**Real-time** WebSocket (or a Discord webhook, to start simple) pushes new matching deals as they're found.

**Frontend** Deal feed, a per-listing "why this is a deal" view showing the comps used, a watchlist, and a price-history chart.

**Deploy** Docker Compose (api, worker, scheduler, postgres+pgvector, redis). Keep a "demo mode" seeded with cached sample data for showing recruiters, since live scraping is fragile to demo cold.

### Practical notes

- **eBay**: official Browse API for active listings. Reliable, so start here.
- **Depop**: no official public API; unofficial endpoints are commonly used for hobby projects. Treat as best-effort, expect breakage.
- **Facebook Marketplace**: ToS prohibits scraping and it's login-walled with strong bot detection. Treat as local-only, personal-use, and don't deploy or demo this part publicly.
- "Recently sold" data generally isn't handed to you, so plan to build it yourself via disappearance-tracking, since it needs time to accumulate.

### Roadmap (build order)

Each stage assumes the ones before it are working end to end. Don't start a stage until you can demonstrate the previous one with real data.

**1. Ingestion + data model**

- [x] Define `Listing` schema, set up Postgres + pgvector
- [x] eBay Browse API connector, normalize, store
- [x] Saved-search config (keyword + location), no UI yet
- [x] Start the disappearance-tracking job (needs time to accumulate data, so get it running as early as possible)

Verified for real on 2026-06-13: Docker Desktop and WSL2 installed, Postgres and Redis running healthy, both migrations applied, all 13 tests passing, and `GET /listings` / `GET /health` responding correctly against the real database. The one piece still unverified is a real eBay API call, since no developer credentials are configured yet. Register at developer.ebay.com, fill in `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` in `.env`, then run `python -m connectors.ingest_ebay` and confirm `GET /listings` returns real rows before calling stage 1 fully done.

**2. Systems layer**

- [ ] Redis job queue + scheduler
- [ ] Rate limiting + backoff per source
- [ ] Dedup logic (source ID + image hash)

**3. Feature pipeline**

- [ ] CLIP embeddings pipeline on listing images, stored in pgvector
- [ ] NLP extraction (brand/model/size/condition)

**4. Valuation engine**

- [ ] k-NN comp retrieval against pgvector
- [ ] Deal scoring logic + confidence weighting

**5. Backend API**

- [ ] FastAPI: auth, saved searches, deal feed endpoint
- [ ] Alerting (Discord webhook to start; upgrade later if time allows)

**6. Frontend**

- [ ] React dashboard: deal feed, comp explanation view, watchlist, price chart

**7. Deploy + polish**

- [ ] Docker Compose full stack, deploy api/worker + frontend
- [ ] Write up README, polish devlog

---

## 2. Future Directions: The Bundle Engine (design now, build later)

Once the Deal Scanner works, several of your ideas turn out to be the same underlying capability applied to different domains: given a **goal** (a budget, a parts list, a style), assemble a **coherent set of items across multiple listings** that satisfies constraints, rather than scoring one listing at a time.

This reuses everything from the core build (listings, embeddings, comps, valuation) and adds two new pieces:

- **Domain constraints**: rules specific to each use case (e.g. PC part compatibility, clothing sizing, budget limits)
- **A selection step**: choosing a compatible, coherent, in-budget set from many candidate listings, closer to a small constraint-solving / optimization problem than a single lookup

### Flavors of the Bundle Engine

**PC build assistant** Given a budget and rough intent ("gaming PC, ~$800"), search for individual components (CPU, GPU, RAM, case, PSU, storage) across listings, and assemble a set that fits the budget and respects compatibility constraints (socket type, wattage headroom, form factor). This is the most concrete of the three, since compatibility rules are well-defined and checkable.

**Room decorating** Given a target style (e.g. "mid-century modern," a color palette, room dimensions), search across furniture/decor categories and assemble a moodboard-like set (sofa, table, lamp, rug) that's stylistically coherent and in budget. Needs style-similarity matching (extending the existing CLIP embeddings to compare a style description against item photos) and cross-category grouping.

**Wardrobe curation** Given clothing preferences (style, size, color palette) and a budget, build a small capsule wardrobe across categories (tops, bottoms, shoes, outerwear), respecting per-user sizing and style coherence. Structurally very similar to room decorating, with sizing as a hard constraint.

### Why defer this

Each flavor needs real domain modeling (compatibility rules, style taxonomies, sizing data) on top of the core pipeline, and none of it is useful until the core Deal Scanner reliably produces good single-item scores. It's worth designing for now, since the schema and embeddings work below already support it, but not worth building in month one.

---

## 3. Presentation Layer

**Phase 1 (in the 4-week plan)**: a web dashboard (React), which is enough to demo and is what most recruiters/interviewers will actually look at.

**Phase 2 (later)**: since the frontend already talks to the backend purely through the API, adding a second client (a PWA or a React Native app) doesn't require backend changes. Treat "make it an app" as a presentation-layer add-on once the core product is solid, not a parallel effort now.

---

## 4. Repo structure

- `README.md`
- `DEVLOG.md`
- `docs/decisions/`: ADRs, one file per real architectural decision
    - `0001-why-a-job-queue.md`
    - `0002-why-pgvector-not-a-separate-vector-db.md`
- `connectors/`: `ebay.py`, `depop.py`, `normalizer.py`
- `systems/`: `queue.py`, `scheduler.py`, `ratelimit.py`
- `ml/`: `embeddings.py`, `nlp_extract.py`, `valuation.py`
- `api/`: `main.py`, `auth.py`, `routes/`
- `frontend/`: React app
- `infra/`: `docker-compose.yml`
- `tests/`

---

## 5. Devlog template

Keep `DEVLOG.md` at the repo root. One entry per work session (not per calendar day). This is your raw material for interview stories later.

```markdown
## 2026-XX-XX - [short title of what you worked on]

**Did:**
-

**Decided:**
- [decision] because [reason]. (Link an ADR if it's a big one.)

**Broke / debugged:**
-

**Next:**
-
```

---

## 6. ADR template

One file per real decision in `docs/decisions/`, numbered. Keep them short, 3-6 sentences is usually enough.

```markdown
# 000X - [Decision title]

**Context:** What problem forced this decision?

**Decision:** What did you choose?

**Alternatives considered:** What else did you weigh, and why not?

**Consequences:** What does this make easier/harder later?
```

Good candidate ADRs for the core build: why a job queue instead of inline processing, why pgvector instead of a dedicated vector DB, how the deal-score confidence weighting works, why disappearance-tracking instead of a sold-data API, how dedup handles cross-source duplicate listings.

---

## 7. README skeleton (write last)

- One-paragraph pitch + demo link/gif
- Architecture overview
- Tech stack
- Setup instructions (`docker-compose up`)
- What the deal score means and how it's computed
- Notable engineering decisions (link to `docs/decisions/`)
- Roadmap / future directions (Bundle Engine, mobile app)

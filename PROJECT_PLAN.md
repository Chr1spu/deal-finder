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
- **Depop**: no official public API; unofficial endpoints are commonly used for hobby projects. Treat as best-effort, expect breakage. Pull-based like eBay: a scheduled connector polls it and gets folded into disappearance-tracking.
- **Facebook Marketplace**: ToS prohibits scraping and it's login-walled with strong bot detection, so there's no server-side connector for it at all. Push-based instead: a browser extension running in the user's own logged-in session captures one listing at a time on click and posts it to the API. See `docs/decisions/0001-multi-source-connector-strategy.md`. No automated disappearance-tracking is possible for Facebook listings, only manual re-capture.
- **OfferUp**: not yet in scope. Deferred until it's actually prioritized; will get the same pull-vs-push evaluation Depop and Facebook Marketplace just got.
- "Recently sold" data generally isn't handed to you, so plan to build it yourself via disappearance-tracking, since it needs time to accumulate. Applies to pull-based sources (eBay, Depop); push-based sources like Facebook rely on manual re-capture instead.

### Roadmap (build order)

Each stage assumes the ones before it are working end to end. Don't start a stage until you can demonstrate the previous one with real data.

**1. Ingestion + data model**

- [x] Define `Listing` schema, set up Postgres + pgvector
- [x] eBay Browse API connector, normalize, store
- [x] Saved-search config (keyword + location), no UI yet
- [x] Start the disappearance-tracking job (needs time to accumulate data, so get it running as early as possible)

Verified for real on 2026-06-13: Docker Desktop and WSL2 installed, Postgres and Redis running healthy, both migrations applied, all 13 tests passing, and `GET /listings` / `GET /health` responding correctly against the real database.

Verified for real on 2026-06-21: real eBay sandbox credentials configured, `python -m connectors.ingest_ebay` run against the live sandbox API, and `GET /listings` returned the real result. Stage 1 is fully done end to end, not just code-complete. Next up is stage 2 (systems layer).

**2. Systems layer**

- [x] Redis job queue + scheduler
- [x] Rate limiting + backoff per source
- [x] Dedup logic (source ID + image hash)

Verified for real on 2026-06-26: migration applied against the real Postgres, a real ingest run against the eBay sandbox (no images on that sandbox item, so nothing to hash there, but the real network+Pillow+imagehash path was confirmed separately against a live image URL), both jobs enqueued against the real Redis and processed by a real `rq worker`. Image-hash dedup is compute-and-store only so far; the cross-source matching logic is deferred until Depop exists, see `docs/decisions/0002-image-hash-dedup.md`. Stage 2 is done. Next is stage 3 (feature pipeline), though `connectors/depop.py` is now unblocked too, per `docs/decisions/0001-multi-source-connector-strategy.md`.

Reopened as stage 2.5 on 2026-07-12: stage 2 was code-complete but had stopped working in production. The disappearance check issued one `get_item` per active listing, which at 10,496 listings wanted ~42,000 eBay calls/day against a measured allowance of 5,000, so it exhausted the daily quota and took ingestion down with it for about 7 hours before anyone looked. Rebuilt to skip listings ingestion has just seen (free proof they're alive, since eBay only returns active listings from search), check the longest-unseen first inside a quota-derived budget, and retire never-ending listings to a new `stale` status. Also added per-search error isolation to `ingest_all` (one 429 was killing all 64 searches) and a real circuit breaker for daily-quota 429s. See `docs/decisions/0003-ebay-call-budget.md`. eBay's bulk `getItems` and Marketplace Insights APIs would both solve this far more cleanly and are both `403` for this application; applying for them is the highest-leverage unblock available.

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

### Post-completion backlog: perfecting the Deal Scanner

Things worth revisiting once stages 1-7 above are actually done, not blockers to finishing them. Roughly ordered by how much they'd actually improve the system, not by ease.

- **Parallelize ingestion.** `ingest_all()` currently runs every saved search sequentially, one at a time, and image-hashing new listings is sequential too, both fully I/O-bound (waiting on network round-trips). A `ThreadPoolExecutor` (or splitting into one RQ job per saved search, drained by multiple workers) could cut real run time drastically. Relatively easy to add given the current code already opens its own DB session per search call; the main care needed is capping concurrency so it doesn't overwhelm eBay's rate limits.
- **Paginate past the 200-result cap.** Each saved search only ever sees eBay's first 200 results per run (its per-call max); a keyword with more active listings than that never has its later results seen at all. Needs looping with eBay's `offset` parameter until a keyword's results are exhausted, with a sane upper bound. As of 2026-07-12 this is at least *measurable*: `SavedSearch.last_result_total` records how many results eBay says exist, so `WHERE last_result_total > 200` names exactly which searches are being truncated. Note the tension with the call budget, though: pagination multiplies ingest cost per search, and ingest calls come out of the same 5,000/day as disappearance checking.
- ~~**Capture shipping cost.**~~ Done 2026-07-12. It turned out to cost nothing: eBay returns `shippingOptions[].shippingCost` in the same payload ingestion already fetches, so this was a normalizer change, not a new API call. Captured alongside `buyingOptions`, which was being dropped the same way and matters more (for an auction, `price` is the current bid, not an asking price, so unflagged auctions would have quietly poisoned stage 4's comps). See `docs/decisions/0004-trustworthy-comp-data.md`.
- **Decide what stage 4 does with auction comps.** Now that `is_auction` exists, the first question to ask of real data is what fraction of the corpus it covers. If it's large, comps need a weighting or exclusion rule; ADR 0004 deliberately doesn't decide this without data.
- **`buy.item.bulk`, if it is obtainable at all.** Both it and Marketplace Insights are Limited Release scopes that this application gets `403` on today, and eBay refuses to mint tokens for either. Marketplace Insights (real sold prices, which is exactly what disappearance tracking infers the hard way) appears to be granted only to established partners and is probably not realistic for a personal project, so **plan as though it is not coming**. `buy.item.bulk` is the more interesting of the two anyway, because it targets the constraint actually being hit: it gates Browse's `getItems`, which takes 20 item ids per call, and per `getRateLimits` it runs on a *separate* 5,000/day meter from search. That would move disappearance checking off the shared budget entirely (~100,000 listing-checks/day) and free the whole Browse allowance for ingest, supporting roughly 400 saved searches instead of 91. Per eBay's Buy API requirements docs, the route is the **eBay Partner Network**, not the developer portal: create an EPN account, submit the Buy API Application, reply to the confirmation email with **mocks and data flows of your user experience**, wait ~10 business days for an approve/decline, then open a Developer Support ticket titled "Buy API Production Access (eBay user ID)". Approval is explicitly not guaranteed, and the Buy APIs are stated to be "intended for eBay partners only".

Two things follow. First, the same docs say access to the Order API, Offer API, **and Marketplace Insights cannot be granted upon request at all**, which settles that question: Insights is not on the menu, and no plan should assume it. `buy.item.bulk` is not in that list, so it does appear obtainable in principle. Second, EPN is an *affiliate* program that evaluates a business model, and the application wants UI mocks, so applying is worth doing **after stage 6's dashboard exists** rather than now with nothing to show. Nothing in the current design depends on this landing, so waiting costs nothing.
- **Monitoring, so a dead pipeline is noticed by something other than a person happening to look.** The 2026-07-12 outage ran for about 7 hours in silence. A failed RQ job looks exactly like an idle one from the outside. Even something crude (a Discord ping on a failed job, which stage 5's alerting work already needs a webhook for) would have caught it immediately.
- **More sophisticated rate limiting at real scale.** The current retry/backoff (`systems/ratelimit.py`) is reactive, per-call. Stage 2.5 added a budget resolved from eBay's live remaining quota, but it's still resolved per pass by a single worker. If saved-search count and worker concurrency both grow a lot, a shared proactive throttle (e.g. a token bucket backed by Redis, since multiple workers would need to agree on one budget) becomes worth it.
- **A data retention/archival policy.** Nothing ever deletes a listing, on purpose, since sold listings are the comp data the valuation engine needs. Table size is a non-issue for a long time at any realistic scale (see DEVLOG), but a real project running for years might eventually want a policy for archiving very old sold listings rather than never revisiting the question.

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
    - `0001-multi-source-connector-strategy.md`
- `connectors/`: `ebay.py`, `depop.py`, `normalizer.py`
- `systems/`: `queue.py`, `scheduler.py`, `ratelimit.py`
- `ml/`: `embeddings.py`, `nlp_extract.py`, `valuation.py`
- `api/`: `main.py`, `auth.py`, `routes/`
- `extension/`: browser extension for push-based capture (Facebook Marketplace first)
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

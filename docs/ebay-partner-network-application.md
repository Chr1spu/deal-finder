# eBay Partner Network application, for `buy.item.bulk`

**Status: drafted, not submitted.** Submitting requires an eBay Partner Network
account in a real person's name and a real eBay user id, so the last step is
the user's to take. Everything the application asks for that can be prepared in
advance is below.

`PROJECT_PLAN.md` deliberately deferred this until stage 6's dashboard existed,
because the application asks for mocks and data flows of the user experience.
It exists now, so the blocker is gone.

---

## Why this scope and not another

Two Limited Release Buy APIs would help this project, and only one is worth
asking for.

**Marketplace Insights** returns real sold prices, which is exactly what
disappearance tracking infers the hard way. **Do not ask for it.** eBay's own
Buy API requirements documentation states that access to the Order API, Offer
API and Marketplace Insights *cannot be granted upon request*. Asking wastes
the application.

**`buy.item.bulk`** is the one to ask for, and it targets the constraint this
project is actually shaped by. It gates Browse's `getItems`, which takes **20
item ids per call**, and per `getRateLimits` it runs on a **separate 5,000
calls/day meter** from search.

## The arithmetic, which is the argument

Today, one meter of 5,000 Browse calls/day covers both jobs:

| | calls/day | note |
|---|---|---|
| ingest | 768 | 64 enabled searches x 12 runs/day, 1 call each |
| disappearance check | 2,800 | 1 `getItem` per listing checked |
| **total** | **3,568** | of 5,000, leaving 1,432 |

The disappearance check is what builds the sold-price history the entire
valuation engine depends on, and it costs one call per listing. That is why
the ceiling on saved searches is ~183 and why ADR 0003 exists at all: in July
2026 this exact job wanted ~42,000 calls/day against 5,000, exhausted the
quota, and took ingestion down with it for seven hours.

With `buy.item.bulk`, the same work costs 20x less and comes off a different
meter entirely:

- disappearance checking moves to its own 5,000/day meter at 20 ids per call,
  which is **~100,000 listing-checks/day** against 2,800 today
- the whole Browse allowance is freed for ingest, supporting roughly **400
  saved searches instead of 183**

Nothing in the current design depends on this landing. It is a multiplier on
coverage, not a prerequisite, which is worth saying plainly in the application
rather than overstating need.

## The route, in order

Per eBay's Buy API requirements, this goes through the **eBay Partner Network**,
not the developer portal.

1. Create an EPN account.
2. Submit the Buy API Application.
3. Reply to the confirmation email with **mocks and data flows of the user
   experience**. Material for this is below.
4. Wait roughly 10 business days for approve or decline.
5. If approved, open a Developer Support ticket titled
   `Buy API Production Access (<eBay user id>)`.

Approval is explicitly not guaranteed, and the Buy APIs are stated to be
"intended for eBay partners only". Plan as though it is not coming.

## What to send them

### One-paragraph description

> Undercut is a personal secondhand-marketplace deal finder. It ingests active
> eBay listings for a set of saved searches, builds a price history by tracking
> when those listings leave the market, and scores new listings against
> comparable ones so a buyer can see what an item is actually worth and why.
> Every score ships with the comparable listings it was computed from. It runs
> against the official Browse API inside a measured call budget and does not
> scrape eBay.

### The data flows to describe

```
saved searches ──► Browse search ──► normalize ──► listing table
                                                        │
   disappearance check ◄────────────────────────────────┘
   getItem per active listing (THE BOTTLENECK: 1 call per listing)
   reads the response body for OUT_OF_STOCK / past itemEndDate,
   marks likely_sold, scores how much to trust it as a sale
                                                        │
                              CLIP image embeddings ────┤
                              title attribute extraction│
                                                        ▼
                                                   valuation
                                    k-NN over comps, gated on sale
                                    confidence, weighted by price
                                    confidence, no estimate under
                                    3 comps
                                                        │
                                                        ▼
                                          dashboard + Discord alerts
```

The single change `buy.item.bulk` makes: the `getItem per active listing` step
becomes `getItems, 20 ids per call, on a separate meter`.

### Screens to attach

All three exist and are the honest state of the product:

1. **Deal feed** (`/`, Deals tab). Ranked listings with the discount, a
   confidence bar, and an expandable panel showing every comparable listing
   the estimate was built from, with its sale and price confidence.
2. **Watchlist** (Watchlist tab). Individual listings followed over time, with
   price history and what happened when they left the market.
3. **Saved searches** (Searches tab). Keyword management showing the live eBay
   call budget each search consumes, which is the clearest possible evidence
   that this application respects the rate limits it is asking to raise.

Screen 3 is the one to lead with. It shows the budget arithmetic in the
product itself.

### Points worth making explicitly

- Official Browse API only. eBay's `robots.txt` disallows `/sch/`, this project
  read it, and declined to scrape on that basis. That decision is written up in
  `CLAUDE.md` and dated.
- A measured call budget with a circuit breaker on daily-quota 429s, built
  after a self-inflicted outage rather than in the abstract (ADR 0003).
- Estimates are presented with their comps and their caveats, never as a bare
  percentage, because comps here are listings that left the market rather than
  confirmed sales.
- Personal, non-commercial use. There is no affiliate revenue model, which is
  worth being upfront about given EPN is an affiliate programme and will
  evaluate it as one. This is the weakest part of the application and should
  not be disguised.

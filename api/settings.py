from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    database_url: str = "postgresql+psycopg://dealfinder:dealfinder@localhost:5432/dealfinder"
    redis_url: str = "redis://localhost:6379/0"

    ebay_env: str = "sandbox"
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_marketplace_id: str = "EBAY_US"

    # 2h rather than hourly. Halving ingest cost (1,536 -> 768 calls/day)
    # roughly doubles how many saved searches fit in the same quota, and
    # breadth of coverage finds more deals than polling the same 64 searches
    # twice as often: eBay sorts by Best Match, not recency, so a new listing
    # doesn't necessarily enter the top 200 the moment it's posted anyway.
    # Worth revisiting once IngestResult's inserted/updated split has a week
    # of real data to show how fast the result set actually turns over.
    ingest_interval_seconds: int = 7200
    disappearance_check_interval_seconds: int = 21600

    # Call budgeting, see docs/decisions/0003-ebay-call-budget.md. eBay gives
    # this app 5,000 Browse calls/day total, shared by search and get_item.
    #
    # The arithmetic these defaults are sized against, with 64 saved searches:
    #   ingest    64 calls/run x 12 runs/day (every 2h)      =   768
    #   checking  700 calls/pass x 4 passes/day (every 6h)   = 2,800
    #                                                   total 3,568 of 5,000
    #
    # quota_reserve is held back for ingestion, which must never be starved by
    # the checker: it is both the product (finding a deal early is most of the
    # value) and by far the cheapest liveness signal available, at 200 listings
    # per call against get_item's 1. It is set above a full day of hourly
    # ingest (1,536) on purpose, so checking stops before ingest ever suffers.
    disappearance_check_budget: int = 700
    quota_reserve: int = 2000

    # A listing seen in a saved search within this window is treated as
    # provably alive and skipped, since eBay only returns active listings.
    # MUST stay comfortably above ingest_interval_seconds, or every listing
    # expires between ingest runs, the whole corpus becomes check candidates
    # on every pass, and the budget above stops meaning anything. That failure
    # is silent, so a test enforces the margin.
    proven_alive_seconds: int = 10800

    # Zombie retirement. Deliberately conservative: retiring too eagerly
    # silently costs comp data, and these thresholds have no real data behind
    # them yet (the corpus is days old). Revisit with a month of history.
    stale_after_days: int = 60
    unseen_after_days: int = 30

    # Sale confidence, see docs/decisions/0005-sale-confidence.md. A
    # disappearance is not a sale, so every inferred sale gets scored rather
    # than trusted. Both of these are guesses until there's real data to fit
    # them against, which is much of the point of storing the breakdown.
    #
    # relist_grace_days: how far back to look for another listing reusing the
    # same photo. Too short misses relists posted before the old one lapsed;
    # too long starts matching genuinely different sales of similar items.
    relist_grace_days: int = 21
    # A listing gone this fast is more likely to have really sold.
    quick_sale_days: float = 7.0
    # How close to its published end date a disappearance counts as "ran to
    # term" (i.e. nobody bought it). Generous because the check runs on a
    # 6-hourly interval, so it notices a disappearance well after it happened.
    scheduled_end_tolerance_hours: float = 12.0


settings = Settings()

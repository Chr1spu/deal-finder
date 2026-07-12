"""eBay Browse API client (official API only, see CLAUDE.md constraints).

Auth: OAuth2 client-credentials grant, cached in-memory until it expires.
Docs: https://developer.ebay.com/api-docs/buy/browse/overview.html
"""

from __future__ import annotations

import time
from typing import NamedTuple

import httpx

from api.settings import settings
from systems.ratelimit import call_with_backoff

# eBay condition IDs to include when excluding "new" listings: refurbished
# (manufacturer/seller, all grades), used (all grades), and for-parts. Omits
# 1000 (New), 1500 (New other), 1750 (New with defects) - this project is a
# secondhand deal finder, and "new" listings have no resale depreciation to
# find a deal in. Verified against the real Browse API (see LEARNING_LOG.md).
NON_NEW_CONDITION_IDS = "2000|2010|2020|2030|2500|3000|4000|5000|6000|7000"

_HOSTS = {
    "sandbox": {
        "auth": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        "browse": "https://api.sandbox.ebay.com/buy/browse/v1",
        "analytics": "https://api.sandbox.ebay.com/developer/analytics/v1_beta",
    },
    "production": {
        "auth": "https://api.ebay.com/identity/v1/oauth2/token",
        "browse": "https://api.ebay.com/buy/browse/v1",
        "analytics": "https://api.ebay.com/developer/analytics/v1_beta",
    },
}

# The rate-limit resource covering both search_items and get_item. They share
# one daily allowance, which is the whole reason the disappearance check has
# to budget itself, see docs/decisions/0003-ebay-call-budget.md.
BROWSE_RATE_LIMIT_RESOURCE = "buy.browse"


class EbayAuthError(RuntimeError):
    pass


class RateLimit(NamedTuple):
    """One resource's daily allowance, straight from eBay rather than guessed.

    reset is when the window rolls over; remaining is what's actually left
    right now, which is the number the disappearance check budgets against.
    """

    limit: int
    remaining: int
    reset: str | None


class SearchResult(NamedTuple):
    """items is capped at whatever `limit` was asked for (200 max); total is
    how many results eBay says actually exist.

    The two are returned together so the gap between them is visible.
    Recording it turns "which saved searches are we truncating?" into a query
    instead of a guess, which is what the deferred pagination work needs.
    """

    items: list[dict]
    total: int


class EbayClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None, env: str | None = None):
        self.client_id = client_id or settings.ebay_client_id
        self.client_secret = client_secret or settings.ebay_client_secret
        self.env = env or settings.ebay_env
        self._hosts = _HOSTS[self.env]
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        # One Client for the life of this instance, so a run of 64 searches
        # reuses one TCP+TLS connection instead of completing a fresh
        # handshake per call. Callers that finish with a client should call
        # close(); a leaked one is bounded by the process, and RQ jobs are
        # short-lived, so this isn't worth a context manager yet.
        self._http = httpx.Client(timeout=30.0)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EbayClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _browse_headers(self, token: str) -> dict[str, str]:
        """Marketplace is sent explicitly rather than relying on eBay's
        default. The default happens to be EBAY_US, which is what's wanted,
        but leaving it implicit means a change on eBay's side would silently
        re-point the whole corpus at another country's market."""
        return {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": settings.ebay_marketplace_id,
        }

    def _get_access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token

        if not self.client_id or not self.client_secret:
            raise EbayAuthError(
                "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set, register an app at "
                "developer.ebay.com and add them to .env"
            )

        def do_request() -> httpx.Response:
            resp = httpx.post(
                self._hosts["auth"],
                auth=(self.client_id, self.client_secret),
                data={
                    "grant_type": "client_credentials",
                    "scope": "https://api.ebay.com/oauth/api_scope",
                },
            )
            resp.raise_for_status()
            return resp

        resp = call_with_backoff(do_request)
        body = resp.json()

        token: str = body["access_token"]
        self._token = token
        self._token_expires_at = time.monotonic() + body["expires_in"] - 60
        # Return the local, not self._token: the attribute is str | None, and
        # returning it directly makes this function's -> str a lie as far as
        # a type checker is concerned.
        return token

    def search_items(self, query: str, limit: int = 200, exclude_new: bool = True) -> SearchResult:
        """Search active listings. Returns raw itemSummaries plus the total
        eBay reports, as a SearchResult.

        limit defaults to 200, eBay Browse API's actual per-call maximum -
        there's no pagination beyond that yet, so a keyword with more than
        200 active matches will still only surface its first 200 per run.
        `total` is returned so that truncation is measurable rather than
        invisible; see SearchResult.

        Results come back in eBay's default Best Match order, deliberately.
        `sort=newlyListed` is tempting for a deal finder, and it does have one
        real advantage: it guarantees every new listing is seen, where Best
        Match may never surface a poorly-ranking one at all. It's still the
        wrong trade here, for two reasons tied to the call budget in
        docs/decisions/0003-ebay-call-budget.md.

        First, it destroys the meaning of a disappearance. The check treats
        "not seen in search recently" as the signal worth spending a call on.
        Under Best Match, dropping out correlates with the listing having
        ended. Under newlyListed, a listing drops out purely because 200
        newer ones exist, which says nothing at all about whether it sold, so
        the budget would go to confirming listings that are almost certainly
        alive.

        Second, Best Match lets a listing that stays relevant stay in
        coverage indefinitely, which means free liveness forever. Under
        newlyListed every listing ages out on a clock, so every listing
        eventually costs a call no matter what.

        The real cost of this choice is coverage, not correctness, and the
        fix for it is pagination (see PROJECT_PLAN.md's backlog) or narrower
        keywords, not a different sort.

        exclude_new defaults to True: this is a secondhand deal finder, and a
        brand-new/sealed listing has no resale depreciation to find a deal
        in. Set False if a search genuinely needs new-condition results too.
        """
        token = self._get_access_token()
        params: dict[str, str | int] = {"q": query, "limit": limit}
        if exclude_new:
            params["filter"] = f"conditionIds:{{{NON_NEW_CONDITION_IDS}}}"

        def do_request() -> httpx.Response:
            resp = self._http.get(
                f"{self._hosts['browse']}/item_summary/search",
                headers=self._browse_headers(token),
                params=params,
            )
            resp.raise_for_status()
            return resp

        resp = call_with_backoff(do_request)
        body = resp.json()
        return SearchResult(items=body.get("itemSummaries", []), total=int(body.get("total", 0)))

    def get_rate_limit(self, resource: str = BROWSE_RATE_LIMIT_RESOURCE) -> RateLimit | None:
        """How much of the daily Browse allowance is actually left.

        Returns None if the answer can't be determined (the Developer
        Analytics API is unreachable, or doesn't report this resource).
        Callers must treat None as "unknown" and fall back to a configured
        budget rather than assuming either zero or infinity.

        Worth the extra call because it's metered separately from Browse, so
        asking costs nothing from the budget it's reporting on. Without it the
        disappearance check would be budgeting against a hardcoded guess at
        the daily limit, and the guess would silently rot if eBay ever changed
        the allowance.
        """
        token = self._get_access_token()

        def do_request() -> httpx.Response:
            resp = self._http.get(
                f"{self._hosts['analytics']}/rate_limit/",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp

        try:
            resp = call_with_backoff(do_request)
        except (httpx.HTTPError, EbayAuthError):
            return None

        for group in resp.json().get("rateLimits", []):
            for res in group.get("resources", []):
                if res.get("name") != resource:
                    continue
                for rate in res.get("rates", []):
                    limit = rate.get("limit")
                    remaining = rate.get("remaining")
                    if limit is None or remaining is None:
                        continue
                    return RateLimit(int(limit), int(remaining), rate.get("reset"))
        return None

    def get_item(self, item_id: str) -> dict | None:
        """Fetch a single item by ID, used by disappearance tracking. Returns
        None if the item is gone (404), which is our signal it likely sold."""
        token = self._get_access_token()

        def do_request() -> httpx.Response:
            resp = self._http.get(
                f"{self._hosts['browse']}/item/{item_id}",
                headers=self._browse_headers(token),
            )
            if resp.status_code != 404:
                resp.raise_for_status()
            return resp

        resp = call_with_backoff(do_request)
        if resp.status_code == 404:
            return None
        return resp.json()

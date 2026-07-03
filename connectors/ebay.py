"""eBay Browse API client (official API only, see CLAUDE.md constraints).

Auth: OAuth2 client-credentials grant, cached in-memory until it expires.
Docs: https://developer.ebay.com/api-docs/buy/browse/overview.html
"""

from __future__ import annotations

import time

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
    },
    "production": {
        "auth": "https://api.ebay.com/identity/v1/oauth2/token",
        "browse": "https://api.ebay.com/buy/browse/v1",
    },
}


class EbayAuthError(RuntimeError):
    pass


class EbayClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None, env: str | None = None):
        self.client_id = client_id or settings.ebay_client_id
        self.client_secret = client_secret or settings.ebay_client_secret
        self.env = env or settings.ebay_env
        self._hosts = _HOSTS[self.env]
        self._token: str | None = None
        self._token_expires_at: float = 0.0

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

        self._token = body["access_token"]
        self._token_expires_at = time.monotonic() + body["expires_in"] - 60
        return self._token

    def search_items(self, query: str, limit: int = 200, exclude_new: bool = True) -> list[dict]:
        """Search active listings. Returns raw itemSummaries from the API.

        limit defaults to 200, eBay Browse API's actual per-call maximum -
        there's no pagination beyond that yet, so a keyword with more than
        200 active matches will still only surface its first 200 per run.

        exclude_new defaults to True: this is a secondhand deal finder, and a
        brand-new/sealed listing has no resale depreciation to find a deal
        in. Set False if a search genuinely needs new-condition results too.
        """
        token = self._get_access_token()
        params = {"q": query, "limit": limit}
        if exclude_new:
            params["filter"] = f"conditionIds:{{{NON_NEW_CONDITION_IDS}}}"

        def do_request() -> httpx.Response:
            resp = httpx.get(
                f"{self._hosts['browse']}/item_summary/search",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            resp.raise_for_status()
            return resp

        resp = call_with_backoff(do_request)
        return resp.json().get("itemSummaries", [])

    def get_item(self, item_id: str) -> dict | None:
        """Fetch a single item by ID, used by disappearance tracking. Returns
        None if the item is gone (404), which is our signal it likely sold."""
        token = self._get_access_token()

        def do_request() -> httpx.Response:
            resp = httpx.get(
                f"{self._hosts['browse']}/item/{item_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 404:
                resp.raise_for_status()
            return resp

        resp = call_with_backoff(do_request)
        if resp.status_code == 404:
            return None
        return resp.json()

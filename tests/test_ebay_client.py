import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from connectors.ebay import EbayAuthError, EbayClient

FIXTURE = Path(__file__).parent / "fixtures" / "ebay_item_summary.json"
AUTH_URL = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search"


def make_client() -> EbayClient:
    return EbayClient(client_id="test-id", client_secret="test-secret", env="sandbox")


@respx.mock
def test_search_items_returns_item_summaries():
    respx.post(AUTH_URL).mock(
        return_value=Response(200, json={"access_token": "fake-token", "expires_in": 7200})
    )
    item = json.loads(FIXTURE.read_text())
    respx.get(SEARCH_URL).mock(
        return_value=Response(200, json={"itemSummaries": [item], "total": 4321})
    )

    result = make_client().search_items("nintendo switch")

    assert result.items == [item]
    assert result.total == 4321, "the reported total is what makes truncation measurable"


@respx.mock
def test_search_items_sends_an_explicit_marketplace_header():
    """Relying on eBay's default would silently re-point the whole corpus at
    another country's market if that default ever changed."""
    respx.post(AUTH_URL).mock(
        return_value=Response(200, json={"access_token": "fake-token", "expires_in": 7200})
    )
    route = respx.get(SEARCH_URL).mock(return_value=Response(200, json={"itemSummaries": []}))

    make_client().search_items("nintendo switch")

    assert route.calls.last.request.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"


@respx.mock
def test_search_items_excludes_new_condition_by_default():
    respx.post(AUTH_URL).mock(
        return_value=Response(200, json={"access_token": "fake-token", "expires_in": 7200})
    )
    route = respx.get(SEARCH_URL).mock(return_value=Response(200, json={"itemSummaries": []}))

    make_client().search_items("rtx 4090")

    sent_filter = route.calls.last.request.url.params["filter"]
    assert sent_filter == "conditionIds:{2000|2010|2020|2030|2500|3000|4000|5000|6000|7000}"
    assert "1000" not in sent_filter, "condition 1000 is New, must not be included"


@respx.mock
def test_search_items_can_opt_out_of_condition_filter():
    respx.post(AUTH_URL).mock(
        return_value=Response(200, json={"access_token": "fake-token", "expires_in": 7200})
    )
    route = respx.get(SEARCH_URL).mock(return_value=Response(200, json={"itemSummaries": []}))

    make_client().search_items("rtx 4090", exclude_new=False)

    assert "filter" not in route.calls.last.request.url.params


@respx.mock
def test_get_item_returns_none_when_listing_is_gone():
    respx.post(AUTH_URL).mock(
        return_value=Response(200, json={"access_token": "fake-token", "expires_in": 7200})
    )
    respx.get("https://api.sandbox.ebay.com/buy/browse/v1/item/v1|123|0").mock(
        return_value=Response(404)
    )

    result = make_client().get_item("v1|123|0")

    assert result is None


def test_search_items_without_credentials_raises_clear_error(monkeypatch):
    """client_id="" alone isn't enough to simulate "no credentials": it's
    falsy, so EbayClient's `client_id or settings.ebay_client_id` fallback
    would silently pick up real credentials from .env if any are configured
    (as they are, since stage 1's sandbox verification). Blank out settings
    itself so this test reflects "nothing configured anywhere," not just
    "nothing passed to the constructor"."""
    monkeypatch.setattr("connectors.ebay.settings.ebay_client_id", "")
    monkeypatch.setattr("connectors.ebay.settings.ebay_client_secret", "")
    client = EbayClient(client_id="", client_secret="", env="sandbox")

    with pytest.raises(EbayAuthError) as excinfo:
        client.search_items("nintendo switch")
    assert "EBAY_CLIENT_ID" in str(excinfo.value)

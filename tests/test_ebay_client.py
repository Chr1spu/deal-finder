import json
from pathlib import Path

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
    respx.get(SEARCH_URL).mock(return_value=Response(200, json={"itemSummaries": [item]}))

    results = make_client().search_items("nintendo switch")

    assert results == [item]


@respx.mock
def test_get_item_returns_none_when_listing_is_gone():
    respx.post(AUTH_URL).mock(
        return_value=Response(200, json={"access_token": "fake-token", "expires_in": 7200})
    )
    respx.get(f"https://api.sandbox.ebay.com/buy/browse/v1/item/v1|123|0").mock(
        return_value=Response(404)
    )

    result = make_client().get_item("v1|123|0")

    assert result is None


def test_search_items_without_credentials_raises_clear_error():
    client = EbayClient(client_id="", client_secret="", env="sandbox")

    try:
        client.search_items("nintendo switch")
        assert False, "expected EbayAuthError"
    except EbayAuthError as e:
        assert "EBAY_CLIENT_ID" in str(e)

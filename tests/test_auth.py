"""API key auth on writes.

The fail-closed behaviour gets the most coverage here, because it is the part
a future reader is most likely to mistake for a bug and "fix" into the
conventional "empty key disables auth", which is the outcome ADR 0017 exists
to prevent.
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from api.db import get_session
from api.main import app
from api.settings import settings

SEARCH_PAYLOAD = {"keyword": "test keyword"}
CAPTURE_PAYLOAD = {
    "source": "depop",
    "source_id": "auth-test-1",
    "title": "a thing",
    "price": 10.0,
    "url": "https://www.depop.com/products/x/",
}


@pytest.fixture()
def client(test_engine, monkeypatch):
    """No API key configured by default: that is the state under test."""
    import api.routes.saved_searches as routes

    monkeypatch.setattr(routes, "engine", test_engine)
    monkeypatch.setattr(settings, "api_key", "")

    def override_get_session():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ------------------------------------------------------------- fails closed


def test_writes_are_refused_when_no_key_is_configured(client):
    """The decision ADR 0017 turns on. An unset secret must not mean an open
    endpoint: the conventional inverse saves five minutes of local setup and
    produces exactly one catastrophic outcome, which is deploying with the
    variable unset and every write endpoint public."""
    response = client.post("/saved-searches", json=SEARCH_PAYLOAD)

    assert response.status_code == 503
    assert "no API_KEY configured" in response.json()["detail"]


def test_the_refusal_explains_how_to_fix_it(client):
    """A 503 that does not say what to set is a dead end."""
    detail = client.post("/saved-searches", json=SEARCH_PAYLOAD).json()["detail"]

    assert "API_KEY" in detail
    assert "X-API-Key" in detail


def test_an_unconfigured_server_refuses_even_with_a_key_supplied(client):
    """Nothing a caller sends can substitute for server configuration."""
    response = client.post(
        "/saved-searches", json=SEARCH_PAYLOAD, headers={"X-API-Key": "anything"}
    )
    assert response.status_code == 503


def test_503_not_401_when_unconfigured(client):
    """The request is not unauthorized, the server is not set up. Conflating
    them sends someone hunting for a credential that does not exist yet."""
    assert client.post("/saved-searches", json=SEARCH_PAYLOAD).status_code == 503


# --------------------------------------------------------- with a key set


def test_writes_need_the_header(client, api_key):
    assert client.post("/saved-searches", json=SEARCH_PAYLOAD).status_code == 401


def test_a_wrong_key_is_rejected(client, api_key):
    response = client.post(
        "/saved-searches", json=SEARCH_PAYLOAD, headers={"X-API-Key": "wrong"}
    )
    assert response.status_code == 401


def test_the_right_key_is_accepted(client, auth_headers):
    response = client.post("/saved-searches", json=SEARCH_PAYLOAD, headers=auth_headers)
    assert response.status_code == 201


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("post", "/saved-searches", SEARCH_PAYLOAD),
        ("patch", "/saved-searches/1", {"enabled": False}),
        ("delete", "/saved-searches/1", None),
        ("post", "/capture", CAPTURE_PAYLOAD),
    ],
)
def test_every_mutating_route_is_protected(client, api_key, method, path, payload):
    """A new write endpoint that forgets the dependency is a silent hole, so
    the whole set is asserted rather than spot-checked."""
    call = getattr(client, method)
    response = call(path, json=payload) if payload is not None else call(path)
    assert response.status_code == 401, f"{method.upper()} {path} is unprotected"


# ------------------------------------------------------------ reads stay open


@pytest.mark.parametrize("path", ["/health", "/listings", "/saved-searches", "/deals"])
def test_reads_do_not_require_a_key(client, api_key, path):
    """Reads expose the user's own corpus, the frontend and the extension both
    need them, and gating them buys little against the threat that matters,
    which is unwanted writes."""
    assert client.get(path).status_code == 200

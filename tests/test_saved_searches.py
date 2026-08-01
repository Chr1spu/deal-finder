"""Saved-search CRUD, and the quota guard that is the whole point of it.

Each enabled search costs one Browse call per ingest run, forever. An
unguarded add is a delayed, silent route back to the outage in ADR 0003, so
the refusal path gets more coverage here than the happy path.
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from api.db import get_session
from api.main import app
from api.models import SavedSearch
from connectors.disappearance_check import EBAY_DAILY_BROWSE_LIMIT


@pytest.fixture()
def client(test_engine, monkeypatch, api_key):
    """Overrides both the dependency and the module-level engine, because the
    saved-search routes open their own Session rather than taking one via
    Depends (they need it inside a transaction with the quota check)."""
    import api.routes.saved_searches as routes

    monkeypatch.setattr(routes, "engine", test_engine)

    def override_get_session():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        # Every write in this file carries the key; auth itself is the subject
        # of tests/test_auth.py.
        test_client.headers.update({"X-API-Key": api_key})
        yield test_client
    app.dependency_overrides.clear()


def seed(test_engine, count: int, enabled: bool = True) -> None:
    with Session(test_engine) as session:
        for n in range(count):
            session.add(SavedSearch(keyword=f"keyword {n}", enabled=enabled))
        session.commit()


# ----------------------------------------------------------------- listing


def test_listing_reports_the_budget_alongside_the_searches(client, test_engine):
    """The cost is shown where the decision is made, not buried in a settings
    comment."""
    seed(test_engine, 3)

    body = client.get("/saved-searches").json()

    assert len(body["searches"]) == 3
    budget = body["budget"]
    assert budget["enabled_searches"] == 3
    assert budget["daily_limit"] == EBAY_DAILY_BROWSE_LIMIT
    assert budget["total_calls_per_day"] > 0
    assert budget["max_searches"] > 3


def test_disabled_searches_do_not_count_toward_the_budget(client, test_engine):
    seed(test_engine, 2, enabled=True)
    seed(test_engine, 5, enabled=False)

    body = client.get("/saved-searches").json()

    assert len(body["searches"]) == 7, "all are listed"
    assert body["budget"]["enabled_searches"] == 2, "only enabled ones cost anything"


def test_listing_can_hide_disabled_searches(client, test_engine):
    seed(test_engine, 2, enabled=True)
    seed(test_engine, 3, enabled=False)

    body = client.get("/saved-searches", params={"include_disabled": False}).json()
    assert len(body["searches"]) == 2


# ---------------------------------------------------------------- creating


def test_creating_a_search(client, test_engine):
    response = client.post("/saved-searches", json={"keyword": "  rtx  4090  "})

    assert response.status_code == 201
    assert response.json()["keyword"] == "rtx 4090", "whitespace normalized"
    assert response.json()["enabled"] is True


def test_duplicate_keywords_are_rejected_case_insensitively(client, test_engine):
    """Two searches differing only in capitalisation return the same eBay
    results and cost twice."""
    client.post("/saved-searches", json={"keyword": "RTX 4090"})
    response = client.post("/saved-searches", json={"keyword": "rtx 4090"})

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_a_blank_keyword_is_rejected(client, test_engine):
    assert client.post("/saved-searches", json={"keyword": "   "}).status_code == 422


# ------------------------------------------------------------ the quota guard


def test_adding_a_search_past_the_ceiling_is_refused(client, test_engine):
    """The reason this endpoint exists at all. A refusal, not a warning: the
    failure mode is delayed and silent, which is exactly the combination that
    took the pipeline down for seven hours in ADR 0003."""
    ceiling = client.get("/saved-searches").json()["budget"]["max_searches"]
    seed(test_engine, ceiling)

    response = client.post("/saved-searches", json={"keyword": "one too many"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["projected_calls_per_day"] > detail["daily_limit"]
    # Actionable rather than a bare refusal.
    assert "disable an existing search" in detail["remedy"]


def test_the_refusal_does_not_persist_the_search(client, test_engine):
    ceiling = client.get("/saved-searches").json()["budget"]["max_searches"]
    seed(test_engine, ceiling)

    client.post("/saved-searches", json={"keyword": "one too many"})

    with Session(test_engine) as session:
        assert not session.exec(
            select(SavedSearch).where(SavedSearch.keyword == "one too many")
        ).all()


def test_disabling_frees_capacity_for_a_new_search(client, test_engine):
    """The remedy the error message advertises has to actually work."""
    ceiling = client.get("/saved-searches").json()["budget"]["max_searches"]
    seed(test_engine, ceiling)
    assert client.post("/saved-searches", json={"keyword": "blocked"}).status_code == 409

    with Session(test_engine) as session:
        first = session.exec(select(SavedSearch)).first()
        first_id = first.id
    client.patch(f"/saved-searches/{first_id}", json={"enabled": False})

    assert client.post("/saved-searches", json={"keyword": "blocked"}).status_code == 201


def test_re_enabling_is_quota_checked_like_creating(client, test_engine):
    """Re-enabling costs the same calls as adding, so it must be gated the
    same way, or the guard is trivially bypassed."""
    ceiling = client.get("/saved-searches").json()["budget"]["max_searches"]
    seed(test_engine, ceiling)
    with Session(test_engine) as session:
        first = session.exec(select(SavedSearch)).first()
        first_id = first.id

    client.patch(f"/saved-searches/{first_id}", json={"enabled": False})
    client.post("/saved-searches", json={"keyword": "took the free slot"})

    response = client.patch(f"/saved-searches/{first_id}", json={"enabled": True})
    assert response.status_code == 409


# ---------------------------------------------------- enabling and deleting


def test_disabling_and_re_enabling_round_trips(client, test_engine):
    created = client.post("/saved-searches", json={"keyword": "gpu"}).json()

    disabled = client.patch(f"/saved-searches/{created['id']}", json={"enabled": False}).json()
    assert disabled["enabled"] is False

    enabled = client.patch(f"/saved-searches/{created['id']}", json={"enabled": True}).json()
    assert enabled["enabled"] is True


def test_deleting_a_search(client, test_engine):
    created = client.post("/saved-searches", json={"keyword": "gpu"}).json()

    assert client.delete(f"/saved-searches/{created['id']}").status_code == 204
    with Session(test_engine) as session:
        assert session.get(SavedSearch, created["id"]) is None


def test_operations_on_a_missing_search_are_404(client, test_engine):
    assert client.patch("/saved-searches/9999", json={"enabled": False}).status_code == 404
    assert client.delete("/saved-searches/9999").status_code == 404


# ------------------------------------------------ the filter that must hold


def test_ingest_skips_disabled_searches(test_engine):
    """The one place a bug here would be invisible: a disabled search that
    still runs simply keeps spending quota, silently and forever."""
    from connectors.ingest_ebay import ingest_all
    from tests.test_ingest_ebay import FakeEbayClient, fake_image_hasher, load_fixture

    with Session(test_engine) as session:
        session.add(SavedSearch(keyword="enabled one", enabled=True))
        session.add(SavedSearch(keyword="disabled one", enabled=False))
        session.commit()

    result = ingest_all(
        client=FakeEbayClient([load_fixture()]),
        db_engine=test_engine,
        image_hasher=fake_image_hasher,
    )

    assert result.searches_run == 1, "the disabled search must cost nothing"

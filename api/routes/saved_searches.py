"""CRUD for the keywords that decide what this system ever sees.

Saved searches are the only lever on coverage, and coverage is the biggest
driver of how many deals exist to be found. They are also not free: each
enabled search costs exactly one Browse call per ingest run, which at a
2-hour interval is 12 calls a day, forever.

So `POST` refuses rather than warns. The failure mode of an unguarded add is
delayed and silent (the quota runs out partway through some later day and
ingestion dies with it), and that exact failure already cost this project
seven hours of downtime once. See docs/decisions/0016-saved-search-crud.md.

SECURITY: unauthenticated, like the rest of the API, which is only acceptable
while the stack is local-only. A reachable POST here is a way to burn someone
else's API quota.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, field_validator
from sqlmodel import Session, col, func, select

from api.auth import RequireApiKey
from api.db import engine
from api.models import SavedSearch
from api.settings import settings
from connectors.disappearance_check import EBAY_DAILY_BROWSE_LIMIT, estimate_daily_calls

router = APIRouter(prefix="/saved-searches", tags=["saved-searches"])
# Writes carry the key; the GET below is deliberately left open. Declared on
# each mutating route rather than the whole router so the read stays public.
WRITE = [RequireApiKey]


class SavedSearchRead(BaseModel):
    id: int | None
    keyword: str
    location: str | None
    enabled: bool
    created_at: datetime
    last_run_at: datetime | None
    # How many results eBay says the query really has. Ingestion only ever
    # sees the first 200, so a large number here means this search is being
    # truncated and would benefit from being narrowed.
    last_result_total: int | None


class Budget(BaseModel):
    """The cost of coverage, shown where the decision is made rather than
    buried in a settings comment."""

    enabled_searches: int
    calls_per_search_per_day: int
    ingest_calls_per_day: int
    check_calls_per_day: int
    total_calls_per_day: int
    daily_limit: int
    remaining_capacity: int
    max_searches: int


class SavedSearchFeed(BaseModel):
    searches: list[SavedSearchRead]
    budget: Budget


class SavedSearchCreate(BaseModel):
    keyword: str
    location: str | None = None

    @field_validator("keyword")
    @classmethod
    def _normalize(cls, value: str) -> str:
        cleaned = " ".join(value.split()).strip()
        if not cleaned:
            raise ValueError("keyword must not be blank")
        if len(cleaned) > 120:
            raise ValueError("keyword is implausibly long for an eBay search")
        return cleaned


class SavedSearchUpdate(BaseModel):
    enabled: bool


def _ingest_runs_per_day() -> int:
    return max(1, 86400 // max(1, settings.ingest_interval_seconds))


def _budget(enabled_count: int) -> Budget:
    """Projected daily cost at a given number of enabled searches.

    Reuses estimate_daily_calls rather than duplicating the arithmetic, so the
    ceiling tracks ingest_interval_seconds and disappearance_check_budget
    automatically: halve the ingest interval and the number of searches that
    fit halves too, with nobody having to remember to update a constant.
    """
    ingest_calls, check_calls = estimate_daily_calls(enabled_count)
    per_search = _ingest_runs_per_day()
    return Budget(
        enabled_searches=enabled_count,
        calls_per_search_per_day=per_search,
        ingest_calls_per_day=ingest_calls,
        check_calls_per_day=check_calls,
        total_calls_per_day=ingest_calls + check_calls,
        daily_limit=EBAY_DAILY_BROWSE_LIMIT,
        remaining_capacity=max(0, EBAY_DAILY_BROWSE_LIMIT - ingest_calls - check_calls),
        max_searches=max(0, (EBAY_DAILY_BROWSE_LIMIT - check_calls) // per_search),
    )


def _enabled_count(session: Session) -> int:
    return int(
        session.exec(
            select(func.count()).select_from(SavedSearch).where(col(SavedSearch.enabled).is_(True))
        ).one()
    )


@router.get("", response_model=SavedSearchFeed)
def list_saved_searches(
    include_disabled: bool = Query(True),
) -> SavedSearchFeed:
    with Session(engine) as session:
        statement = select(SavedSearch).order_by(col(SavedSearch.id).asc())
        if not include_disabled:
            statement = statement.where(col(SavedSearch.enabled).is_(True))
        searches = session.exec(statement).all()
        return SavedSearchFeed(
            searches=[SavedSearchRead.model_validate(s, from_attributes=True) for s in searches],
            budget=_budget(_enabled_count(session)),
        )


@router.post(
    "",
    response_model=SavedSearchRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=WRITE,
)
def create_saved_search(payload: SavedSearchCreate) -> SavedSearchRead:
    """Add a search, unless it would push daily calls past the allowance.

    A refusal rather than a warning, and the error body carries the arithmetic
    so it is actionable: what it would cost, what is left, and that disabling
    an existing search frees exactly one search's worth.
    """
    with Session(engine) as session:
        # Case-insensitive: two searches differing only in capitalisation
        # return the same eBay results and cost twice.
        existing = session.exec(
            select(SavedSearch).where(func.lower(SavedSearch.keyword) == payload.keyword.lower())
        ).first()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"'{existing.keyword}' already exists as saved search {existing.id}",
            )

        projected = _budget(_enabled_count(session) + 1)
        if projected.total_calls_per_day > EBAY_DAILY_BROWSE_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "adding this search would exceed the daily eBay call allowance",
                    "projected_calls_per_day": projected.total_calls_per_day,
                    "daily_limit": EBAY_DAILY_BROWSE_LIMIT,
                    "max_searches": projected.max_searches,
                    "remedy": (
                        "disable an existing search to free "
                        f"{projected.calls_per_search_per_day} calls/day, or raise "
                        "INGEST_INTERVAL_SECONDS so each search costs less"
                    ),
                },
            )

        search = SavedSearch(keyword=payload.keyword, location=payload.location)
        session.add(search)
        session.commit()
        session.refresh(search)
        return SavedSearchRead.model_validate(search, from_attributes=True)


@router.patch("/{search_id}", response_model=SavedSearchRead, dependencies=WRITE)
def update_saved_search(search_id: int, payload: SavedSearchUpdate) -> SavedSearchRead:
    """Enable or disable. Re-enabling is quota-checked exactly like creating,
    since it costs the same calls."""
    with Session(engine) as session:
        search = session.get(SavedSearch, search_id)
        if search is None:
            raise HTTPException(status_code=404, detail=f"no saved search with id {search_id}")

        if payload.enabled and not search.enabled:
            projected = _budget(_enabled_count(session) + 1)
            if projected.total_calls_per_day > EBAY_DAILY_BROWSE_LIMIT:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "re-enabling this search would exceed the daily allowance",
                        "projected_calls_per_day": projected.total_calls_per_day,
                        "daily_limit": EBAY_DAILY_BROWSE_LIMIT,
                    },
                )

        search.enabled = payload.enabled
        session.add(search)
        session.commit()
        session.refresh(search)
        return SavedSearchRead.model_validate(search, from_attributes=True)


@router.delete(
    "/{search_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # Explicit, because FastAPI would otherwise infer a response model from
    # the `-> Response` annotation and refuse: a 204 must carry no body.
    response_class=Response,
    dependencies=WRITE,
)
def delete_saved_search(search_id: int) -> Response:
    """Delete permanently.

    Prefer PATCH with enabled=false: it frees the same quota and keeps
    last_result_total and last_run_at, which are accumulated observability
    rather than config. Listings already ingested by this search are NOT
    touched, since they are comp data and belong to the corpus, not to the
    query that happened to find them.
    """
    with Session(engine) as session:
        search = session.get(SavedSearch, search_id)
        if search is None:
            raise HTTPException(status_code=404, detail=f"no saved search with id {search_id}")
        session.delete(search)
        session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

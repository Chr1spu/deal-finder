"""Retry-with-backoff for outbound HTTP calls to any source. Source-agnostic
so eBay today and Depop later can both wrap their httpx calls with it.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

T = TypeVar("T")

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# eBay returns 429 for two very different situations: "you're going too fast
# right now" (retry works) and "you've used your whole daily allowance"
# (retry cannot possibly work until the quota window resets). Both arrive as
# 429; only the body tells them apart, via errorId 2001. Verified against the
# real production API, see docs/decisions/0003-ebay-call-budget.md.
QUOTA_EXHAUSTED_ERROR_IDS = {2001}


class QuotaExhaustedError(RuntimeError):
    """The daily API allowance is gone. Retrying is worse than useless: it
    burns more of an allowance that's already at zero, and the window won't
    reset for hours. Callers should stop the run and report what they got
    done, not treat this as a transient blip."""


def _is_quota_exhausted(response: httpx.Response) -> bool:
    """True if a 429 body says 'daily allowance gone' rather than 'slow down'.

    Deliberately forgiving: a 429 whose body isn't JSON, or is JSON in an
    unexpected shape, is treated as an ordinary retryable rate limit. Guessing
    "quota exhausted" from an unparseable body would abort a whole run over
    what might be a transient blip.
    """
    try:
        body = response.json()
    except (ValueError, TypeError):
        return False
    if not isinstance(body, dict):
        return False
    return any(
        isinstance(err, dict) and err.get("errorId") in QUOTA_EXHAUSTED_ERROR_IDS
        for err in body.get("errors", [])
    )


def call_with_backoff(
    fn: Callable[[], T],
    max_attempts: int = 5,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Calls fn(), retrying on rate-limit/server errors with exponential
    backoff plus jitter. Non-retryable errors (other 4xx, anything that
    isn't an httpx error at all) propagate immediately.

    A 429 that means "daily quota exhausted" is raised straight away as
    QuotaExhaustedError instead of being retried, see _is_quota_exhausted.
    """

    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and _is_quota_exhausted(e.response):
                raise QuotaExhaustedError(
                    "eBay daily call quota exhausted (errorId 2001), stopping this run"
                ) from e
            if e.response.status_code not in RETRYABLE_STATUS_CODES or attempt >= max_attempts:
                raise
        except httpx.TransportError:
            if attempt >= max_attempts:
                raise

        delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, base_delay)
        sleep(delay)

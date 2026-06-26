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


def call_with_backoff(
    fn: Callable[[], T],
    max_attempts: int = 5,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Calls fn(), retrying on rate-limit/server errors with exponential
    backoff plus jitter. Non-retryable errors (other 4xx, anything that
    isn't an httpx error at all) propagate immediately."""

    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in RETRYABLE_STATUS_CODES or attempt >= max_attempts:
                raise
        except httpx.TransportError:
            if attempt >= max_attempts:
                raise

        delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, base_delay)
        sleep(delay)

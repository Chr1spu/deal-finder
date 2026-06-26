import httpx
import pytest

from systems.ratelimit import call_with_backoff


def make_response(status_code: int) -> httpx.Response:
    request = httpx.Request("GET", "https://example.test/")
    return httpx.Response(status_code, request=request)


def error_for(status_code: int) -> httpx.HTTPStatusError:
    resp = make_response(status_code)
    return httpx.HTTPStatusError("error", request=resp.request, response=resp)


def test_retries_then_succeeds_on_retryable_status():
    sleeps = []
    attempts = {"count": 0}

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise error_for(503)
        return "ok"

    result = call_with_backoff(flaky, max_attempts=5, base_delay=0.01, sleep=sleeps.append)

    assert result == "ok"
    assert attempts["count"] == 3
    assert len(sleeps) == 2, "should sleep once per failed attempt, not on the final success"


def test_non_retryable_status_raises_immediately():
    attempts = {"count": 0}

    def always_401():
        attempts["count"] += 1
        raise error_for(401)

    with pytest.raises(httpx.HTTPStatusError):
        call_with_backoff(always_401, max_attempts=5, base_delay=0.01, sleep=lambda _: None)

    assert attempts["count"] == 1, "a non-retryable error must not be retried at all"


def test_raises_after_exhausting_max_attempts():
    attempts = {"count": 0}

    def always_503():
        attempts["count"] += 1
        raise error_for(503)

    with pytest.raises(httpx.HTTPStatusError):
        call_with_backoff(always_503, max_attempts=3, base_delay=0.01, sleep=lambda _: None)

    assert attempts["count"] == 3


def test_transport_error_is_retried():
    attempts = {"count": 0}

    def flaky_network():
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", "https://example.test/"))
        return "ok"

    result = call_with_backoff(flaky_network, max_attempts=3, base_delay=0.01, sleep=lambda _: None)

    assert result == "ok"
    assert attempts["count"] == 2

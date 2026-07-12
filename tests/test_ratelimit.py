import httpx
import pytest

from systems.ratelimit import QuotaExhaustedError, call_with_backoff


def make_response(status_code: int, json_body: dict | None = None) -> httpx.Response:
    request = httpx.Request("GET", "https://example.test/")
    if json_body is None:
        return httpx.Response(status_code, request=request)
    return httpx.Response(status_code, request=request, json=json_body)


def error_for(status_code: int, json_body: dict | None = None) -> httpx.HTTPStatusError:
    resp = make_response(status_code, json_body)
    return httpx.HTTPStatusError("error", request=resp.request, response=resp)


# eBay's real daily-quota body, copied from a live 429 during stage 2.5.
QUOTA_EXHAUSTED_BODY = {
    "errors": [
        {
            "errorId": 2001,
            "domain": "ACCESS",
            "category": "REQUEST",
            "message": "Too many requests.",
            "longMessage": "The request limit has been reached for the resource.",
        }
    ]
}


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


# --- quota exhaustion vs. ordinary rate limiting -------------------------
# Both arrive as 429. Only the body tells them apart, and getting this wrong
# is what let one exhausted quota take the whole pipeline down for hours:
# every call retried 5x, burning 5x the allowance it needed.


def test_daily_quota_exhaustion_is_not_retried():
    attempts = {"count": 0}
    sleeps = []

    def quota_gone():
        attempts["count"] += 1
        raise error_for(429, QUOTA_EXHAUSTED_BODY)

    with pytest.raises(QuotaExhaustedError):
        call_with_backoff(quota_gone, max_attempts=5, base_delay=0.01, sleep=sleeps.append)

    assert attempts["count"] == 1, "retrying an exhausted daily quota only burns more of it"
    assert sleeps == [], "and there's nothing to wait for, the window resets in hours"


def test_ordinary_429_without_a_quota_error_id_is_still_retried():
    """A plain 'slow down' 429 is exactly what backoff is for. Only errorId
    2001 means the daily allowance is actually gone."""
    attempts = {"count": 0}

    def throttled():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise error_for(429, {"errors": [{"errorId": 1234, "message": "Slow down"}]})
        return "ok"

    result = call_with_backoff(throttled, max_attempts=5, base_delay=0.01, sleep=lambda _: None)

    assert result == "ok"
    assert attempts["count"] == 3


@pytest.mark.parametrize(
    "body",
    [None, {"errors": []}, {"unexpected": "shape"}, {"errors": ["not-a-dict"]}],
    ids=["no-json", "empty-errors", "wrong-shape", "non-dict-error"],
)
def test_unparseable_429_body_is_treated_as_retryable(body):
    """Deliberately forgiving. Guessing 'quota exhausted' from a body we can't
    read would abort a whole run over what might be a transient blip, so an
    unrecognizable 429 falls back to ordinary backoff."""
    attempts = {"count": 0}

    def odd_429():
        attempts["count"] += 1
        raise error_for(429, body)

    with pytest.raises(httpx.HTTPStatusError):
        call_with_backoff(odd_429, max_attempts=2, base_delay=0.01, sleep=lambda _: None)

    assert attempts["count"] == 2, "retried like a normal 429, not raised as QuotaExhaustedError"

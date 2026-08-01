"""API key auth on writes.

Reads stay open: they expose the user's own corpus, and the frontend and the
extension's match display both need them. What needs protecting is writes,
and specifically `POST /saved-searches`, because each accepted search costs 12
eBay Browse calls a day forever against a 5,000/day allowance. Someone who can
add searches can starve ingestion without exfiltrating anything, which is the
same outage docs/decisions/0003 documents.

**Fails closed.** An unset key refuses writes rather than allowing them. The
conventional alternative (empty key means auth disabled) optimises for the
first five minutes of local development and produces exactly one catastrophic
outcome: deploy, forget the variable, every write endpoint public with nothing
anywhere to notice. See docs/decisions/0017-api-key-auth.md.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, status

from api.settings import settings

API_KEY_HEADER = "X-API-Key"

NOT_CONFIGURED_DETAIL = (
    "This API has no API_KEY configured, so writing is disabled. Set API_KEY in .env "
    "(any long random string) and send it as an X-API-Key header. Writes fail closed "
    "on purpose: an unset secret must not mean an open endpoint."
)
MISSING_KEY_DETAIL = f"Missing {API_KEY_HEADER} header."
BAD_KEY_DETAIL = f"Invalid {API_KEY_HEADER}."


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency for any endpoint that mutates state.

    503 rather than 401 when no key is configured, deliberately: the request
    is not unauthorized, the server is not set up, and conflating the two
    would send someone hunting for a credential that does not exist yet.
    """
    if not settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED_DETAIL
        )

    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=MISSING_KEY_DETAIL,
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )

    # Constant-time. Habit rather than necessity at this scale, but it costs
    # nothing and means this file does not model a timing leak for whoever
    # copies it next.
    if not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=BAD_KEY_DETAIL)


# Applied at the router level rather than per-route, so a new write endpoint
# is protected by default and forgetting the dependency is not a silent hole.
RequireApiKey = Depends(require_api_key)

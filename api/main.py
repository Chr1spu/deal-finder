from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.capture import router as capture_router
from api.routes.deals import router as deals_router
from api.routes.listings import router as listings_router
from api.routes.saved_searches import router as saved_searches_router

app = FastAPI(title="Deal Finder API")

# The browser extension posts captures from depop.com and facebook.com pages,
# so those origins have to be allowed explicitly. Deliberately a fixed list
# rather than "*": this API is unauthenticated while it stays local-only, and
# a wildcard would let any page the user visits write to their database.
# See docs/decisions/0010-depop-is-push-based-now.md.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.depop.com",
        "https://depop.com",
        "https://www.facebook.com",
        "https://web.facebook.com",
    ],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
)

app.include_router(listings_router)
app.include_router(capture_router)
app.include_router(deals_router)
app.include_router(saved_searches_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

from fastapi import FastAPI

from api.routes.listings import router as listings_router

app = FastAPI(title="Deal Finder API")

app.include_router(listings_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

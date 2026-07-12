from collections.abc import Sequence

from fastapi import APIRouter, Depends
from sqlmodel import Session, col, select

from api.db import get_session
from api.models import Listing

router = APIRouter(prefix="/listings", tags=["listings"])


@router.get("", response_model=list[Listing])
def list_listings(session: Session = Depends(get_session)) -> Sequence[Listing]:
    return session.exec(select(Listing).order_by(col(Listing.first_seen_at).desc())).all()

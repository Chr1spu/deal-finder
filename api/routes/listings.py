from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from api.db import get_session
from api.models import Listing

router = APIRouter(prefix="/listings", tags=["listings"])


@router.get("", response_model=list[Listing])
def list_listings(session: Session = Depends(get_session)) -> list[Listing]:
    return session.exec(select(Listing).order_by(Listing.first_seen_at.desc())).all()

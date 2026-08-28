from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from bangumi_local import __version__
from bangumi_local.web.dependencies import get_session


router = APIRouter()


@router.get("/health")
def health(session: Session = Depends(get_session)) -> dict[str, object]:
    session.execute(text("SELECT 1")).scalar_one()
    return {"status": "ok", "database": "reachable", "version": __version__}

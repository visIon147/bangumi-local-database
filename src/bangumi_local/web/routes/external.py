from __future__ import annotations

from fastapi import APIRouter, Depends, Path
from fastapi.responses import RedirectResponse

from bangumi_local.config import Settings
from bangumi_local.web.dependencies import get_settings


router = APIRouter(prefix="/out")


@router.get("/bangumi/{subject_id}")
def open_bangumi_subject(
    subject_id: int = Path(..., ge=1),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    return RedirectResponse(
        f"{settings.bangumi_web_base_url}/subject/{subject_id}", status_code=307
    )

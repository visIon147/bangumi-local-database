from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    BangumiCollectionState,
    ChangePlan,
    DiscoverySession,
    RatingQueueSession,
    Work,
)
from bangumi_local.web.dependencies import get_session


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    counts = {
        "works": session.scalar(select(func.count()).select_from(Work)) or 0,
        "collections": session.scalar(
            select(func.count()).select_from(BangumiCollectionState)
        )
        or 0,
        "draft_plans": session.scalar(
            select(func.count()).select_from(ChangePlan).where(ChangePlan.status == "draft")
        )
        or 0,
        "reviewed_plans": session.scalar(
            select(func.count())
            .select_from(ChangePlan)
            .where(ChangePlan.status.in_(("reviewed", "partial")))
        )
        or 0,
        "rating_queues": session.scalar(
            select(func.count())
            .select_from(RatingQueueSession)
            .where(RatingQueueSession.status == "active")
        )
        or 0,
        "discovery_sessions": session.scalar(
            select(func.count())
            .select_from(DiscoverySession)
            .where(DiscoverySession.status == "active")
        )
        or 0,
    }
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"counts": counts, "page_title": "概览"},
    )

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from bangumi_local.config import Settings
from bangumi_local.db.models import ChangePlan, UiJob, UiJobPlanLink
from bangumi_local.services.backups import BackupError, backup_sqlite_database
from bangumi_local.services.workspace_history import (
    WorkspaceHistoryError,
    archive_jobs,
    archive_plans,
    preview_job_purge,
    preview_plan_purge,
    purge_jobs,
    purge_plans,
)
from bangumi_local.web.dependencies import get_session, get_settings, get_write_session


router = APIRouter(prefix="/workspace")
_PAGE_SIZES = (25, 50, 100)


def _archive_filter(statement, model, archived: str):
    if archived == "only":
        return statement.where(model.archived_at.is_not(None))
    if archived == "all":
        return statement
    return statement.where(model.archived_at.is_(None))


def _job_next_action(session: Session, job: UiJob) -> tuple[str, str] | None:
    row = session.execute(
        select(UiJobPlanLink, ChangePlan)
        .join(ChangePlan, ChangePlan.id == UiJobPlanLink.plan_id)
        .where(UiJobPlanLink.job_id == job.id)
        .order_by(UiJobPlanLink.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    link, plan = row
    if plan.kind == "steam_match" and plan.status == "draft":
        return "等待人工匹配审核", f"/plans/{plan.id}"
    if plan.status == "draft":
        return "计划待审阅", f"/plans/{plan.id}"
    if plan.status in {"reviewed", "partial"}:
        return "计划待执行", f"/plans/{plan.id}"
    return f"关联计划：{plan.status}", f"/plans/{plan.id}"


@router.get("", response_class=HTMLResponse)
def workspace(
    request: Request,
    view: str = Query("jobs"),
    status: str = Query(""),
    q: str = Query("", max_length=200),
    archived: str = Query("active"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    if view not in {"jobs", "plans"} or archived not in {"active", "only", "all"}:
        raise HTTPException(422, "Unsupported workspace filter.")
    if page_size not in _PAGE_SIZES:
        raise HTTPException(422, "Unsupported page size.")
    normalized_q = q.strip()
    if view == "jobs":
        base = select(UiJob)
        count = select(func.count()).select_from(UiJob)
        if status:
            base = base.where(UiJob.status == status)
            count = count.where(UiJob.status == status)
        if normalized_q:
            term = f"%{normalized_q}%"
            predicate = or_(UiJob.id.like(term), UiJob.kind.like(term))
            base = base.where(predicate)
            count = count.where(predicate)
        base = _archive_filter(base, UiJob, archived)
        count = _archive_filter(count, UiJob, archived)
        total = int(session.scalar(count) or 0)
        rows = list(
            session.scalars(
                base.order_by(UiJob.created_at.desc(), UiJob.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        items = tuple(
            {"row": row, "next_action": _job_next_action(session, row)} for row in rows
        )
        statuses = tuple(session.scalars(select(UiJob.status).distinct().order_by(UiJob.status)))
    else:
        base = select(ChangePlan)
        count = select(func.count()).select_from(ChangePlan)
        if status:
            base = base.where(ChangePlan.status == status)
            count = count.where(ChangePlan.status == status)
        if normalized_q:
            term = f"%{normalized_q}%"
            predicate = or_(
                ChangePlan.id.like(term),
                ChangePlan.kind.like(term),
                ChangePlan.operation.like(term),
            )
            base = base.where(predicate)
            count = count.where(predicate)
        base = _archive_filter(base, ChangePlan, archived)
        count = _archive_filter(count, ChangePlan, archived)
        total = int(session.scalar(count) or 0)
        rows = list(
            session.scalars(
                base.order_by(ChangePlan.created_at.desc(), ChangePlan.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        items = tuple({"row": row, "next_action": None} for row in rows)
        statuses = tuple(
            session.scalars(select(ChangePlan.status).distinct().order_by(ChangePlan.status))
        )
    page_count = max(1, (total + page_size - 1) // page_size)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="workspace/index.html",
        context={
            "view": view,
            "items": items,
            "statuses": statuses,
            "status": status,
            "q": normalized_q,
            "archived": archived,
            "page": min(page, page_count),
            "page_size": page_size,
            "page_sizes": _PAGE_SIZES,
            "page_count": page_count,
            "total": total,
            "page_title": "任务与计划工作台",
        },
    )


def _history_redirect(view: str, archived: str = "active") -> RedirectResponse:
    return RedirectResponse(f"/workspace?view={view}&archived={archived}", status_code=303)


@router.post("/jobs/archive")
def workspace_archive_jobs(
    ids: list[str] = Form(...), session: Session = Depends(get_write_session)
) -> RedirectResponse:
    try:
        archive_jobs(session, tuple(ids), archived=True)
    except WorkspaceHistoryError as exc:
        raise HTTPException(409, str(exc)) from None
    return _history_redirect("jobs")


@router.post("/jobs/restore")
def workspace_restore_jobs(
    ids: list[str] = Form(...), session: Session = Depends(get_write_session)
) -> RedirectResponse:
    try:
        archive_jobs(session, tuple(ids), archived=False)
    except WorkspaceHistoryError as exc:
        raise HTTPException(409, str(exc)) from None
    return _history_redirect("jobs", "only")


@router.post("/plans/archive")
def workspace_archive_plans(
    ids: list[str] = Form(...), session: Session = Depends(get_write_session)
) -> RedirectResponse:
    try:
        archive_plans(session, tuple(ids), archived=True)
    except WorkspaceHistoryError as exc:
        raise HTTPException(409, str(exc)) from None
    return _history_redirect("plans")


@router.post("/plans/restore")
def workspace_restore_plans(
    ids: list[str] = Form(...), session: Session = Depends(get_write_session)
) -> RedirectResponse:
    try:
        archive_plans(session, tuple(ids), archived=False)
    except WorkspaceHistoryError as exc:
        raise HTTPException(409, str(exc)) from None
    return _history_redirect("plans", "only")


@router.post("/{kind}/purge-preview", response_class=HTMLResponse)
def workspace_purge_preview(
    kind: str,
    request: Request,
    ids: list[str] = Form(...),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    if kind not in {"jobs", "plans"}:
        raise HTTPException(404, "Unknown workspace history type.")
    preview = (
        preview_job_purge(session, tuple(ids))
        if kind == "jobs"
        else preview_plan_purge(session, tuple(ids))
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="workspace/purge.html",
        context={
            "kind": kind,
            "preview": preview,
            "confirmation": f"DELETE {len(preview.purgeable)}",
            "page_title": "确认永久删除",
        },
    )


@router.post("/{kind}/purge")
def workspace_purge(
    kind: str,
    request: Request,
    ids: list[str] = Form(...),
    confirmation: str = Form(...),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if kind not in {"jobs", "plans"}:
        raise HTTPException(404, "Unknown workspace history type.")
    with request.app.state.session_factory() as session:
        preview = (
            preview_job_purge(session, tuple(ids))
            if kind == "jobs"
            else preview_plan_purge(session, tuple(ids))
        )
    if preview.blocked or not preview.purgeable:
        raise HTTPException(409, "Selected history is not safe to delete.")
    if confirmation != f"DELETE {len(preview.purgeable)}":
        raise HTTPException(400, "Delete confirmation does not match.")
    try:
        backup_sqlite_database(
            settings.database_url,
            settings.backup_directory,
            label=f"before-{kind}-purge",
        )
    except BackupError as exc:
        raise HTTPException(409, str(exc)) from None
    try:
        with request.app.state.session_factory.begin() as session:
            if kind == "jobs":
                purge_jobs(session, tuple(ids))
            else:
                purge_plans(session, tuple(ids))
    except WorkspaceHistoryError as exc:
        raise HTTPException(409, str(exc)) from None
    return _history_redirect(kind, "only")

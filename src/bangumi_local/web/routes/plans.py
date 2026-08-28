from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from bangumi_local.db.models import ChangePlan, ChangePlanItem, PlanApplyRun, RemoteOperation
from bangumi_local.services.plans import PlanError, export_plan, load_plan
from bangumi_local.services.plan_revisions import revise_plan_selection
from bangumi_local.web.action_protocol import invalid_action
from bangumi_local.web.dependencies import get_session
from bangumi_local.web.routes.media import local_media_url


router = APIRouter(prefix="/plans")
PLAN_STATUSES = {"draft", "reviewed", "applying", "applied", "partial", "failed", "cancelled"}


@router.get("", response_class=HTMLResponse)
def plans_list(
    request: Request,
    status: str | None = Query(None),
    kind: str | None = Query(None),
    q: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    normalized_status = status if status in PLAN_STATUSES else None
    count_statement = select(func.count()).select_from(ChangePlan)
    statement = select(ChangePlan).order_by(ChangePlan.created_at.desc(), ChangePlan.id)
    if normalized_status:
        count_statement = count_statement.where(ChangePlan.status == normalized_status)
        statement = statement.where(ChangePlan.status == normalized_status)
    normalized_kind = (kind or "").strip()
    normalized_q = q.strip()
    if normalized_kind:
        count_statement = count_statement.where(ChangePlan.kind == normalized_kind)
        statement = statement.where(ChangePlan.kind == normalized_kind)
    if normalized_q:
        term = f"%{normalized_q}%"
        predicate = or_(ChangePlan.id.like(term), ChangePlan.kind.like(term), ChangePlan.operation.like(term))
        count_statement = count_statement.where(predicate)
        statement = statement.where(predicate)
    total = session.scalar(count_statement) or 0
    plans = session.scalars(
        statement.offset((page - 1) * page_size).limit(page_size)
    ).all()
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="plans/list.html",
        context={
            "plans": plans,
            "statuses": sorted(PLAN_STATUSES),
            "status": normalized_status or "",
            "kind": normalized_kind,
            "q": normalized_q,
            "kinds": tuple(session.scalars(select(ChangePlan.kind).distinct().order_by(ChangePlan.kind)).all()),
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_previous": page > 1,
            "has_next": page * page_size < total,
            "page_title": "计划中心",
        },
    )


@router.get("/{plan_id}", response_class=HTMLResponse)
def plan_detail(
    plan_id: str,
    request: Request,
    session: Session = Depends(get_session),
    disposition: str = Query(""),
    reason: str = Query(""),
    q: str = Query(""),
) -> HTMLResponse:
    try:
        stored = load_plan(session, plan_id)
    except PlanError as exc:
        status_code = 404 if str(exc).startswith("Plan not found") else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from None
    runs = session.scalars(
        select(PlanApplyRun)
        .where(PlanApplyRun.plan_id == plan_id)
        .order_by(PlanApplyRun.started_at.desc())
    ).all()
    operations = session.scalars(
        select(RemoteOperation)
        .where(RemoteOperation.plan_id == plan_id)
        .order_by(RemoteOperation.started_at.desc())
    ).all()
    item_rows = session.scalars(
        select(ChangePlanItem).where(ChangePlanItem.plan_id == plan_id)
    ).all()
    row_by_subject = {row.subject_id: row for row in item_rows if row.subject_id is not None}
    normalized_disposition = disposition if disposition in {"planned", "unchanged"} else ""
    normalized_reason = reason.strip()
    normalized_q = q.strip().casefold()
    visible = []
    for candidate in stored.candidates:
        if normalized_disposition and candidate.disposition != normalized_disposition:
            continue
        if normalized_reason and candidate.reason != normalized_reason:
            continue
        if normalized_q and normalized_q not in f"{candidate.title} {candidate.subject_id or ''}".casefold():
            continue
        row = row_by_subject.get(candidate.subject_id)
        match_candidates = []
        raw_match_candidates = candidate.selection_evidence.get("match_candidates", [])
        if isinstance(raw_match_candidates, list):
            for raw in raw_match_candidates:
                if not isinstance(raw, dict):
                    continue
                raw_subject_id = raw.get("subject_id")
                try:
                    match_subject_id = int(raw_subject_id)
                except (TypeError, ValueError):
                    continue
                match_candidates.append(
                    {
                        **raw,
                        "cover_src": local_media_url(
                            session,
                            request.app.state.settings.media_cache_directory,
                            subject_id=match_subject_id,
                        ),
                    }
                )
        visible.append(
            {
                "candidate": candidate,
                "item_status": row.item_status if row is not None else "not_applicable",
                "error": row.error if row is not None else None,
                "cover_src": local_media_url(
                    session,
                    request.app.state.settings.media_cache_directory,
                    work_id=candidate.work_id,
                    subject_id=candidate.subject_id,
                    library_entry_id=candidate.source_entry_id,
                ),
                "match_candidates": match_candidates,
            }
        )
    selector = json.loads(stored.plan.selector_json)
    visible_subject_ids = {
        item["candidate"].subject_id for item in visible
        if item["candidate"].subject_id is not None
    }
    hidden_included_ids = tuple(
        candidate.subject_id
        for candidate in stored.planned
        if candidate.subject_id is not None and candidate.subject_id not in visible_subject_ids
    )
    successor = session.scalar(
        select(ChangePlan)
        .where(ChangePlan.selector_json.like(f'%"revision_of_plan_id":"{plan_id}"%'))
        .order_by(ChangePlan.created_at.desc())
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="plans/detail.html",
        context={
            "stored": stored,
            "runs": runs,
            "operations": operations,
            "visible_items": visible,
            "hidden_included_ids": hidden_included_ids,
            "selector": selector,
            "successor": successor,
            "revision_supported": stored.plan.kind in {
                "bulk_add", "bulk_remove", "bulk_rename", "classify_games", "sync",
                "steam_status", "discovery_status", "pull",
            } and stored.plan.status == "draft",
            "disposition_filter": normalized_disposition,
            "reason_filter": normalized_reason,
            "q": q.strip(),
            "reasons": sorted({candidate.reason for candidate in stored.candidates}),
            "page_title": f"计划 {stored.plan.id}",
        },
    )


@router.post("/{plan_id}/revise")
async def revise_plan_action(plan_id: str, request: Request) -> HTMLResponse:
    form = await request.form()
    included: set[int] = set()
    for value in form.getlist("include_subject"):
        try:
            included.add(int(str(value)))
        except ValueError:
            return invalid_action(request, "计划条目 Subject ID 无效。")
    fields: dict[int, tuple[str, ...]] = {}
    decisions: dict[int, str] = {}
    for key in form:
        if key.startswith("field_"):
            try:
                subject_id = int(key.removeprefix("field_"))
            except ValueError:
                return invalid_action(request, "同步字段选择无效。")
            fields[subject_id] = tuple(str(value) for value in form.getlist(key))
        elif key.startswith("classification_"):
            try:
                subject_id = int(key.removeprefix("classification_"))
            except ValueError:
                return invalid_action(request, "分类条目无效。")
            decision = str(form.get(key) or "")
            if decision and decision not in {"galgame", "game", "defer", "exclude"}:
                return invalid_action(request, "分类决定无效。")
            if decision:
                decisions[subject_id] = decision
    try:
        with request.app.state.session_factory.begin() as session:
            successor = revise_plan_selection(
                session,
                plan_id,
                included_subject_ids=included,
                selected_fields=fields,
                classification_decisions=decisions,
            )
            successor_id = successor.plan.id
        with request.app.state.session_factory() as session:
            export_plan(load_plan(session, successor_id), request.app.state.settings.plan_directory)
    except PlanError as exc:
        return invalid_action(request, str(exc), status_code=409)
    except SQLAlchemyError:
        return invalid_action(request, "创建 successor 计划失败。", status_code=500)
    return RedirectResponse(f"/plans/{successor_id}?notice=successor", status_code=303)

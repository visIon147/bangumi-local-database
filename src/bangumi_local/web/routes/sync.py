from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy import select
import json

from bangumi_local.domain.models import CollectionStatus, SubjectType
from bangumi_local.domain.mutations import CollectionPatch, MutationValidationError
from bangumi_local.domain.snapshots import CANONICAL_FIELDS
from bangumi_local.services.edit_collection import edit_local_collection
from bangumi_local.services.jobs import enqueue_job
from bangumi_local.services.status import build_status_report
from bangumi_local.web.action_protocol import invalid_action, job_required
from bangumi_local.web.dependencies import get_session
from bangumi_local.db.models import SyncConflict
from bangumi_local.services.conflicts import ConflictResolutionError, resolve_conflict


router = APIRouter(prefix="/sync")
STATUS_VALUES = {item.label: int(item) for item in CollectionStatus}


def _subject_type(value: str | None) -> SubjectType | None:
    if not value:
        return None
    return SubjectType.parse(value)


def _positive_ids(value: str) -> tuple[int, ...]:
    try:
        ids = tuple(dict.fromkeys(int(item.strip()) for item in value.split(",") if item.strip()))
    except ValueError as exc:
        raise ValueError("Subject ID 必须是逗号分隔的正整数。") from exc
    if not ids or any(item < 1 for item in ids):
        raise ValueError("至少提供一个正整数 Subject ID。")
    return ids


@router.get("", response_class=HTMLResponse)
def sync_home(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="sync/index.html",
        context={
            "page_title": "同步",
            "subject_types": tuple(item.kind for item in SubjectType),
            "fields": CANONICAL_FIELDS,
        },
    )


@router.get("/status", response_class=HTMLResponse)
def cached_status(
    request: Request,
    subject_id: int | None = Query(None, ge=1),
    subject_type: str | None = Query(None),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    try:
        parsed_type = _subject_type(subject_type)
    except ValueError as exc:
        return invalid_action(request, str(exc))
    report = build_status_report(
        session,
        None,
        subject_id=subject_id,
        subject_type=parsed_type,
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="sync/status.html",
        context={"page_title": "同步状态", "report": report},
    )


@router.get("/conflicts", response_class=HTMLResponse)
def conflicts_home(
    request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    conflicts = session.scalars(
        select(SyncConflict)
        .where(SyncConflict.status == "open")
        .order_by(SyncConflict.subject_id, SyncConflict.field, SyncConflict.id)
    ).all()
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="sync/conflicts.html",
        context={"page_title": "冲突处理", "conflicts": conflicts},
    )


@router.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict_action(
    conflict_id: int,
    request: Request,
    strategy: str = Form(...),
    custom_json: str = Form(""),
) -> HTMLResponse:
    try:
        custom_value = json.loads(custom_json) if strategy == "custom" else None
    except json.JSONDecodeError:
        return invalid_action(request, "自定义值必须是合法 JSON。")
    try:
        with request.app.state.session_factory.begin() as session:
            resolve_conflict(
                session,
                conflict_id,
                strategy=strategy,
                custom_value=custom_value,
            )
    except ConflictResolutionError as exc:
        return invalid_action(request, str(exc), status_code=409)
    return RedirectResponse("/sync/conflicts", status_code=303)


@router.get("/collection/{subject_id}/edit", response_class=HTMLResponse)
def edit_collection_form(
    subject_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    from bangumi_local.db.models import BangumiCollectionState

    state = session.get(BangumiCollectionState, subject_id)
    if state is None:
        return invalid_action(request, "该 Bangumi 收藏不存在。", status_code=404)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="sync/edit_collection.html",
        context={
            "page_title": f"编辑收藏 {subject_id}",
            "subject_id": subject_id,
            "state": state,
            "statuses": STATUS_VALUES,
        },
    )


@router.post("/collection/{subject_id}/edit")
def edit_collection_action(
    subject_id: int,
    request: Request,
    rating: int | None = Form(None),
    clear_rating: bool = Form(False),
    status: str | None = Form(None),
    comment: str | None = Form(None),
    clear_comment: bool = Form(False),
    privacy: str = Form("unchanged"),
) -> HTMLResponse:
    values: dict[str, object] = {}
    if clear_rating:
        values["rate"] = 0
    elif rating is not None:
        values["rate"] = rating
    if status:
        if status not in STATUS_VALUES:
            return invalid_action(request, "收藏状态无效。")
        values["type"] = STATUS_VALUES[status]
    if clear_comment:
        values["comment"] = ""
    elif comment is not None and comment != "":
        values["comment"] = comment
    if privacy not in {"unchanged", "private", "public"}:
        return invalid_action(request, "隐私选项无效。")
    if privacy != "unchanged":
        values["private"] = privacy == "private"
    try:
        patch = CollectionPatch(values)
        with request.app.state.session_factory.begin() as session:
            result = edit_local_collection(session, subject_id, patch)
    except (LookupError, MutationValidationError) as exc:
        return invalid_action(request, str(exc))
    except SQLAlchemyError:
        return invalid_action(request, "本地数据库操作失败。", status_code=500)
    changed = ",".join(result.changed_fields) if result.changed_fields else "no-op"
    return RedirectResponse(
        f"/sync/collection/{subject_id}/edit?notice={changed}", status_code=303
    )


@router.post("/pull")
def pull_action(
    request: Request,
    subject_type: str | None = Form(None),
    image_policy: str = Form("metadata"),
) -> HTMLResponse:
    try:
        _subject_type(subject_type)
    except ValueError as exc:
        return invalid_action(request, str(exc))
    if image_policy not in {"none", "metadata", "missing", "refresh"}:
        return invalid_action(request, "图片策略无效。")
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="bangumi_pull_plan",
            capability="remote_read",
            config={"subject_type": subject_type or None, "image_policy": image_policy},
        )
        job_id = job.id
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/status/refresh")
def refresh_status_action(
    request: Request,
    subject_id: int | None = Form(None),
    subject_type: str | None = Form(None),
) -> HTMLResponse:
    if subject_id is not None and subject_id < 1:
        return invalid_action(request, "Subject ID 必须为正整数。")
    try:
        _subject_type(subject_type)
    except ValueError as exc:
        return invalid_action(request, str(exc))
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="status_refresh",
            capability="remote_read",
            config={"subject_id": subject_id, "subject_type": subject_type or None},
        )
        job_id = job.id
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/shadow/bootstrap")
def shadow_bootstrap_action(
    request: Request,
    subject_id: int | None = Form(None),
    apply: bool = Form(False),
) -> HTMLResponse:
    if subject_id is not None and subject_id < 1:
        return invalid_action(request, "Subject ID 必须为正整数。")
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="shadow_bootstrap",
            capability="remote_read" if not apply else "local_write",
            config={"subject_id": subject_id, "apply": apply},
        )
        job_id = job.id
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/plan")
def sync_plan_action(
    request: Request,
    selector_mode: str = Form(...),
    subject_ids: str = Form(""),
    fields: list[str] = Form(...),
) -> HTMLResponse:
    if selector_mode not in {"ids", "all_local_changes"}:
        return invalid_action(request, "同步选择器无效。")
    if selector_mode == "ids":
        try:
            _positive_ids(subject_ids)
        except ValueError as exc:
            return invalid_action(request, str(exc))
    selected = tuple(dict.fromkeys(fields))
    if not selected or set(selected) - set(CANONICAL_FIELDS):
        return invalid_action(request, "同步字段无效。")
    selector = (
        {"mode": "ids", "ids": list(_positive_ids(subject_ids))}
        if selector_mode == "ids"
        else {"mode": "all_local_changes"}
    )
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="sync_plan",
            capability="remote_read",
            config={"selector": selector, "fields": list(selected)},
        )
        job_id = job.id
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)

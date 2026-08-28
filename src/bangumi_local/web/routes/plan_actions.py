from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.exc import SQLAlchemyError

from bangumi_local.services.plans import PlanError, load_plan, review_plan
from bangumi_local.services.jobs import (
    JobError,
    consume_plan_confirmation,
    enqueue_job,
    issue_plan_confirmation,
)
from bangumi_local.db.models import UiJob
from bangumi_local.web.action_protocol import invalid_action, job_required


router = APIRouter(prefix="/plan-actions")


def _exact_plan_id(request: Request, plan_id: str, confirmation: str) -> HTMLResponse | None:
    if not confirmation or not confirmation.isascii() or confirmation != plan_id:
        return invalid_action(
            request,
            "必须输入与当前计划完全一致的完整 Plan ID；未发生任何状态变化。",
        )
    return None


@router.get("/{plan_id}", response_class=HTMLResponse)
def actions_home(plan_id: str, request: Request) -> HTMLResponse:
    try:
        with request.app.state.session_factory() as session:
            stored = load_plan(session, plan_id)
    except PlanError as exc:
        return invalid_action(request, str(exc), status_code=404)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="plans/actions.html",
        context={"page_title": f"计划操作 {plan_id}", "stored": stored},
    )


@router.post("/{plan_id}/review")
def review_action(
    plan_id: str,
    request: Request,
    confirmation_plan_id: str = Form(...),
) -> HTMLResponse:
    rejected = _exact_plan_id(request, plan_id, confirmation_plan_id)
    if rejected is not None:
        return rejected
    try:
        with request.app.state.session_factory.begin() as session:
            review_plan(session, plan_id)
    except PlanError as exc:
        return invalid_action(request, str(exc), status_code=409)
    except SQLAlchemyError:
        return invalid_action(request, "本地数据库操作失败。", status_code=500)
    return RedirectResponse(f"/plans/{plan_id}?notice=reviewed", status_code=303)


@router.post("/{plan_id}/preflight")
def preflight_action(plan_id: str, request: Request) -> HTMLResponse:
    try:
        with request.app.state.session_factory() as session:
            stored = load_plan(session, plan_id)
    except PlanError as exc:
        return invalid_action(request, str(exc), status_code=404)
    if stored.plan.status not in {"reviewed", "partial"}:
        return invalid_action(request, "仅 reviewed 或 partial 计划可进入 fresh preflight。", status_code=409)
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="plan_preflight",
            capability="remote_read",
            config={"plan_id": plan_id},
        )
        job_id = job.id
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/{plan_id}/confirmation")
def issue_confirmation_action(
    plan_id: str,
    request: Request,
    preflight_job_id: str = Form(...),
) -> HTMLResponse:
    try:
        with request.app.state.session_factory.begin() as session:
            job = session.get(UiJob, preflight_job_id)
            if job is None or job.kind != "plan_preflight" or job.status != "succeeded":
                raise JobError("Fresh preflight job is missing or not successful.")
            result = json.loads(job.result_json or "{}")
            if result.get("plan_id") != plan_id:
                raise JobError("Fresh preflight belongs to another plan.")
            if int(result.get("will_modify_count", 0)) < 1:
                raise JobError("Fresh preflight found no safe items to apply.")
            nonce = issue_plan_confirmation(
                session,
                plan_id=plan_id,
                browser_session=request.state.csrf_token,
            )
    except (JobError, ValueError, json.JSONDecodeError) as exc:
        return invalid_action(request, str(exc), status_code=422)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="plans/confirmation.html",
        context={
            "page_title": "Apply 确认",
            "plan_id": plan_id,
            "preflight_job_id": preflight_job_id,
            "nonce": nonce,
            "result": result,
            "submit_action": f"/plan-actions/{plan_id}/apply",
            "operation_label": "Apply",
        },
    )


@router.post("/{plan_id}/manual-uncollect-preflight")
def manual_uncollect_preflight_action(
    plan_id: str, request: Request
) -> Response:
    try:
        with request.app.state.session_factory() as session:
            stored = load_plan(session, plan_id)
    except PlanError as exc:
        return invalid_action(request, str(exc), status_code=404)
    if stored.plan.format_version < 3 or stored.plan.kind != "reverse":
        return invalid_action(
            request,
            "网页取消收藏核对只接受 v3+ reverse 计划。",
            status_code=409,
        )
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="manual_uncollect_preflight",
            capability="remote_read",
            config={"plan_id": plan_id},
        )
        job_id = job.id
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/{plan_id}/manual-uncollect-confirmation")
def issue_manual_uncollect_confirmation(
    plan_id: str,
    request: Request,
    preflight_job_id: str = Form(...),
) -> HTMLResponse:
    try:
        with request.app.state.session_factory.begin() as session:
            job = session.get(UiJob, preflight_job_id)
            if (
                job is None
                or job.kind != "manual_uncollect_preflight"
                or job.status != "succeeded"
            ):
                raise JobError("Manual-uncollect preflight is missing or unsuccessful.")
            result = json.loads(job.result_json or "{}")
            if result.get("plan_id") != plan_id:
                raise JobError("Fresh verification belongs to another plan.")
            if int(result.get("verified_absent_count", 0)) < 1:
                raise JobError("No remotely absent collection was verified.")
            nonce = issue_plan_confirmation(
                session,
                plan_id=plan_id,
                browser_session=request.state.csrf_token,
            )
    except (JobError, ValueError, json.JSONDecodeError) as exc:
        return invalid_action(request, str(exc), status_code=422)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="plans/confirmation.html",
        context={
            "page_title": "网页取消收藏确认",
            "plan_id": plan_id,
            "preflight_job_id": preflight_job_id,
            "nonce": nonce,
            "result": {
                "will_modify_count": result["verified_absent_count"],
                "unchanged_count": 0,
                "will_modify": result["subjects"],
                "unchanged": [],
            },
            "submit_action": f"/plan-actions/{plan_id}/manual-uncollect",
            "operation_label": "核对并对齐 LOCAL",
        },
    )


@router.post("/{plan_id}/apply")
def apply_action(
    plan_id: str,
    request: Request,
    confirmation_plan_id: str = Form(...),
    confirmation_nonce: str = Form(""),
) -> HTMLResponse:
    rejected = _exact_plan_id(request, plan_id, confirmation_plan_id)
    if rejected is not None:
        return rejected
    try:
        with request.app.state.session_factory() as session:
            stored = load_plan(session, plan_id)
    except PlanError as exc:
        return invalid_action(request, str(exc), status_code=404)
    if stored.plan.status not in {"reviewed", "partial"}:
        return invalid_action(request, "计划状态不允许 apply。", status_code=409)
    if stored.plan.kind == "steam_match":
        return job_required(
            request,
            action="steam-match-apply",
            detail="Steam match 使用本地映射专用 preflight/worker；通用远端 apply worker 不会接管该计划。",
        )
    if any(
        str(item.action.get("operation")) in {"manual_uncollect", "delete_collection"}
        for item in stored.planned
    ):
        return invalid_action(
            request,
            "公共 v0 不支持 DELETE；请使用网页取消收藏核对流程。",
            status_code=409,
        )
    if not confirmation_nonce:
        return invalid_action(request, "缺少 fresh preflight 签发的一次性确认 nonce。", status_code=422)
    try:
        with request.app.state.session_factory.begin() as session:
            consume_plan_confirmation(
                session,
                plan_id=plan_id,
                full_plan_id=confirmation_plan_id,
                browser_session=request.state.csrf_token,
                nonce=confirmation_nonce,
            )
            job = enqueue_job(
                session,
                kind="plan_apply",
                capability=(
                    "local_write"
                    if stored.plan.kind == "pull" and stored.plan.format_version == 5
                    else "remote_write"
                ),
                config={"plan_id": plan_id},
                idempotency_key=(
                    "plan-apply:" + plan_id + ":" + hashlib.sha256(
                        confirmation_nonce.encode("utf-8")
                    ).hexdigest()
                ),
            )
            job_id = job.id
    except JobError as exc:
        return invalid_action(request, str(exc), status_code=422)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/{plan_id}/recovery")
def recovery_action(plan_id: str, request: Request) -> HTMLResponse:
    try:
        with request.app.state.session_factory() as session:
            load_plan(session, plan_id)
    except PlanError as exc:
        return invalid_action(request, str(exc), status_code=404)
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="plan_recovery",
            capability="remote_read",
            config={"plan_id": plan_id},
        )
        job_id = job.id
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/{plan_id}/manual-uncollect")
def manual_uncollect_action(
    plan_id: str,
    request: Request,
    confirmation_plan_id: str = Form(...),
    confirmation_nonce: str = Form(""),
) -> HTMLResponse:
    rejected = _exact_plan_id(request, plan_id, confirmation_plan_id)
    if rejected is not None:
        return rejected
    if not confirmation_nonce:
        return invalid_action(request, "缺少网页取消收藏 fresh 验证的一次性 nonce。")
    try:
        with request.app.state.session_factory.begin() as session:
            load_plan(session, plan_id)
            consume_plan_confirmation(
                session,
                plan_id=plan_id,
                full_plan_id=confirmation_plan_id,
                browser_session=request.state.csrf_token,
                nonce=confirmation_nonce,
            )
            job = enqueue_job(
                session,
                kind="manual_uncollect_reconcile",
                capability="local_write",
                config={"plan_id": plan_id},
                idempotency_key=(
                    "manual-uncollect:" + plan_id + ":" + hashlib.sha256(
                        confirmation_nonce.encode("utf-8")
                    ).hexdigest()
                ),
            )
            job_id = job.id
    except (PlanError, JobError) as exc:
        return invalid_action(request, str(exc), status_code=422)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from bangumi_local.db.models import ChangePlan, UiJob, UiJobEvent, UiJobPlanLink
from bangumi_local.services.jobs import enqueue_job, request_job_cancel
from bangumi_local.web.dependencies import get_session, get_write_session


router = APIRouter(prefix="/jobs")
_JOB_STATUSES = {
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancel_requested",
    "cancelled",
    "interrupted",
}
_JOB_KIND_LABELS = {
    "auth_check": "Bangumi 认证检查",
    "bangumi_pull": "Bangumi 收藏拉取",
    "bangumi_pull_plan": "Bangumi Pull 预览计划",
    "bulk_tag_plan": "批量 Tag 计划",
    "classify_games_plan": "游戏分类计划",
    "discovery_create_browse": "Bangumi 有界浏览",
    "discovery_create_search": "Bangumi 探索搜索",
    "discovery_cache_images": "补齐探索候选图片",
    "discovery_status_draft": "探索收藏状态计划",
    "plan_apply": "执行计划",
    "plan_preflight": "计划 Fresh Preflight",
    "rating_queue_create_enriched": "建立并补全评分队列",
    "rating_queue_enrich": "补全评分队列资料",
    "rating_sync_plan": "评分同步计划",
    "remote_media_fetch": "补齐远端图片",
    "steam_import_apply": "应用 Steam 本地导入",
    "steam_import_preview": "Steam 导入预览",
    "steam_match_plan": "Steam 匹配计划",
    "steam_match_search": "Steam 单项候选搜索",
    "steam_media_scan": "扫描 Steam 本地图片",
    "steam_covers_complete": "补全 Steam 远程封面",
    "steam_status_plan": "Steam 状态计划",
    "steam_titles_complete": "补齐 Steam 标题",
    "sync_plan": "通用同步计划",
}
_RESULT_LABELS = {
    "remote_count": "远端读取",
    "imported": "新增收藏",
    "remote_updates": "远端更新",
    "conflicts": "冲突",
    "missing_remote": "远端缺失",
    "planned": "将修改",
    "unchanged": "不修改",
    "subject_count": "Subject 数量",
    "candidate_count": "候选数量",
    "items": "队列项目",
    "enriched": "已补全",
    "failed_enrichment": "补全失败",
    "images_cached": "新缓存图片",
    "already_cached": "复用缓存图片",
    "image_failures": "图片失败",
    "with_cover": "有封面来源",
    "selected": "已选择",
    "cached": "已缓存",
    "failed": "失败",
    "stale": "Stale",
    "applied": "已执行",
    "status": "结果状态",
    "run_id": "执行批次",
    "app_id": "Steam AppID",
    "subject_id": "Bangumi Subject ID",
    "work_id": "本地 Work ID",
    "title": "标题",
    "reconciled": "已对齐",
    "verified_absent_count": "确认已取消收藏",
    "examined": "已检查",
    "requested": "发起补全",
    "already_available": "已有有效封面",
    "no_metadata": "无可用元数据",
    "still_missing": "仍缺封面的 Steam AppID",
}
_PLAN_RESULT_KEYS = {
    "plan_id": "打开计划工作台",
    "reverse_plan_id": "打开反向计划",
    "restore_plan_id": "打开恢复计划",
    "source_plan_id": "打开来源计划",
    "supersedes": "打开被替代计划",
}


def _present_job_result(
    kind: str, result: object
) -> tuple[list[tuple[str, object]], list[tuple[str, str]], list[dict[str, object]]]:
    if not isinstance(result, dict):
        return [], [], []
    facts: list[tuple[str, object]] = []
    links: list[tuple[str, str]] = []
    tables: list[dict[str, object]] = []
    for key, value in result.items():
        if key == "plan_id" and isinstance(result.get("plan_url"), str):
            continue
        if key == "plan_url" and isinstance(value, str) and value.startswith("/plans/"):
            links.append(("打开计划并恢复审核位置", value))
            continue
        if key in _PLAN_RESULT_KEYS and isinstance(value, str) and value:
            links.append((_PLAN_RESULT_KEYS[key], f"/plans/{value}"))
            continue
        if key == "session_id" and isinstance(value, str) and value:
            prefix = "/rating/queues/" if kind.startswith("rating_") else "/discovery/sessions/"
            links.append(("打开队列", f"{prefix}{value}"))
            continue
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            columns = tuple(dict.fromkeys(column for item in value for column in item))
            tables.append(
                {
                    "title": _RESULT_LABELS.get(key, key.replace("_", " ").title()),
                    "columns": columns,
                    "rows": value,
                }
            )
            continue
        if isinstance(value, list):
            tables.append(
                {
                    "title": _RESULT_LABELS.get(key, key.replace("_", " ").title()),
                    "columns": ("value",),
                    "rows": [{"value": item} for item in value],
                }
            )
            continue
        if not isinstance(value, (dict, list)):
            facts.append((_RESULT_LABELS.get(key, key.replace("_", " ").title()), value))
    return facts, links, tables


@router.get("", response_class=HTMLResponse)
def jobs_list(
    request: Request,
    status: str | None = Query(None),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    normalized = status if status in _JOB_STATUSES else None
    statement = (
        select(UiJob)
        .where(UiJob.archived_at.is_(None))
        .order_by(UiJob.created_at.desc())
        .limit(100)
    )
    if normalized:
        statement = statement.where(UiJob.status == normalized)
    jobs = session.scalars(statement).all()
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="jobs/list.html",
        context={
            "jobs": jobs,
            "kind_labels": _JOB_KIND_LABELS,
            "status": normalized or "",
            "statuses": sorted(_JOB_STATUSES),
            "page_title": "任务",
        },
    )


@router.get("/{job_id}", response_class=HTMLResponse)
def job_detail(
    job_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    job = session.get(UiJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    events = session.scalars(
        select(UiJobEvent)
        .where(UiJobEvent.job_id == job_id)
        .order_by(UiJobEvent.sequence)
    ).all()
    result = json.loads(job.result_json) if job.result_json else None
    result_facts, result_links, result_tables = _present_job_result(job.kind, result)
    linked_plans = session.execute(
        select(UiJobPlanLink, ChangePlan)
        .join(ChangePlan, ChangePlan.id == UiJobPlanLink.plan_id)
        .where(UiJobPlanLink.job_id == job_id)
        .order_by(UiJobPlanLink.id)
    ).all()
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="jobs/detail.html",
        context={
            "job": job,
            "events": events,
            "result": result,
            "job_title": _JOB_KIND_LABELS.get(job.kind, job.kind.replace("_", " ")),
            "result_facts": result_facts,
            "result_links": result_links,
            "result_tables": result_tables,
            "linked_plans": linked_plans,
            "page_title": f"任务 {job.id}",
        },
    )


@router.get("/{job_id}/status", response_class=JSONResponse)
def job_status(job_id: str, session: Session = Depends(get_session)) -> JSONResponse:
    job = session.get(UiJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    links = [
        {"plan_id": plan_id, "relation": relation, "url": f"/plans/{plan_id}"}
        for plan_id, relation in session.execute(
            select(UiJobPlanLink.plan_id, UiJobPlanLink.relation)
            .where(UiJobPlanLink.job_id == job_id)
            .order_by(UiJobPlanLink.id)
        )
    ]
    return JSONResponse(
        {
            "id": job.id,
            "status": job.status,
            "phase": job.phase,
            "current": job.progress_current,
            "total": job.progress_total,
            "error": job.error_message,
            "links": links,
        }
    )


@router.post("/pull")
def enqueue_pull(
    subject_type: str = Form(""),
    image_policy: str = Form("metadata"),
    session: Session = Depends(get_write_session),
) -> RedirectResponse:
    if subject_type not in {"", "book", "anime", "music", "game", "real"}:
        raise HTTPException(422, "Unsupported subject type")
    if image_policy not in {"none", "metadata", "cache"}:
        raise HTTPException(422, "Unsupported image policy")
    job = enqueue_job(
        session,
        kind="bangumi_pull",
        capability="remote_read",
        config={"subject_type": subject_type or None, "image_policy": image_policy},
    )
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/{job_id}/cancel")
def cancel_job(
    job_id: str, session: Session = Depends(get_write_session)
) -> RedirectResponse:
    try:
        request_job_cancel(session, job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    ChangePlan,
    LibraryMatchReview,
    PlanApplyRun,
    RemoteOperation,
    UiJob,
    UiJobPlanLink,
)
from bangumi_local.db.repositories import utc_now_iso


class WorkspaceHistoryError(ValueError):
    pass


_TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}
_PURGEABLE_PLAN_STATUSES = {"draft", "cancelled"}


@dataclass(frozen=True, slots=True)
class PurgePreview:
    selected: tuple[str, ...]
    purgeable: tuple[str, ...]
    blocked: tuple[tuple[str, str], ...]


def _unique_ids(ids: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in ids if value.strip()))


def archive_jobs(session: Session, ids: tuple[str, ...], *, archived: bool) -> int:
    selected = _unique_ids(ids)
    rows = list(session.scalars(select(UiJob).where(UiJob.id.in_(selected))))
    if len(rows) != len(selected):
        raise WorkspaceHistoryError("One or more selected jobs do not exist.")
    if archived:
        non_terminal = next(
            (row for row in rows if row.status not in _TERMINAL_JOB_STATUSES), None
        )
        if non_terminal is not None:
            raise WorkspaceHistoryError(f"Job {non_terminal.id} is not in a terminal state.")
    now = utc_now_iso() if archived else None
    for row in rows:
        row.archived_at = now
    return len(rows)


def archive_plans(session: Session, ids: tuple[str, ...], *, archived: bool) -> int:
    selected = _unique_ids(ids)
    rows = list(session.scalars(select(ChangePlan).where(ChangePlan.id.in_(selected))))
    if len(rows) != len(selected):
        raise WorkspaceHistoryError("One or more selected plans do not exist.")
    now = utc_now_iso() if archived else None
    for row in rows:
        row.archived_at = now
    return len(rows)


def preview_job_purge(session: Session, ids: tuple[str, ...]) -> PurgePreview:
    selected = _unique_ids(ids)
    purgeable: list[str] = []
    blocked: list[tuple[str, str]] = []
    for job_id in selected:
        row = session.get(UiJob, job_id)
        if row is None:
            blocked.append((job_id, "任务不存在"))
        elif row.archived_at is None:
            blocked.append((job_id, "必须先归档"))
        elif row.status not in _TERMINAL_JOB_STATUSES:
            blocked.append((job_id, "任务尚未终止"))
        elif session.scalar(
            select(func.count()).select_from(UiJobPlanLink).where(UiJobPlanLink.job_id == job_id)
        ):
            blocked.append((job_id, "仍有关联计划"))
        else:
            purgeable.append(job_id)
    return PurgePreview(selected, tuple(purgeable), tuple(blocked))


def _plan_purge_blocker(session: Session, plan: ChangePlan) -> str | None:
    if plan.archived_at is None:
        return "必须先归档"
    if plan.status not in _PURGEABLE_PLAN_STATUSES:
        return "只有未执行的 draft/cancelled 可以永久删除"
    checks = (
        (PlanApplyRun, PlanApplyRun.plan_id == plan.id, "存在执行批次"),
        (RemoteOperation, RemoteOperation.plan_id == plan.id, "存在远端操作审计"),
        (LibraryMatchReview, LibraryMatchReview.plan_id == plan.id, "存在匹配审核记录"),
        (UiJobPlanLink, UiJobPlanLink.plan_id == plan.id, "仍有关联任务"),
    )
    for model, predicate, message in checks:
        if session.scalar(select(func.count()).select_from(model).where(predicate)):
            return message
    if session.scalar(
        select(func.count()).select_from(ChangePlan).where(ChangePlan.reverse_of_plan_id == plan.id)
    ):
        return "被反向计划引用"
    revision_marker = f'%"revision_of_plan_id":"{plan.id}"%'
    if session.scalar(
        select(func.count()).select_from(ChangePlan).where(ChangePlan.selector_json.like(revision_marker))
    ):
        return "被 successor 计划引用"
    return None


def preview_plan_purge(session: Session, ids: tuple[str, ...]) -> PurgePreview:
    selected = _unique_ids(ids)
    purgeable: list[str] = []
    blocked: list[tuple[str, str]] = []
    for plan_id in selected:
        row = session.get(ChangePlan, plan_id)
        if row is None:
            blocked.append((plan_id, "计划不存在"))
            continue
        reason = _plan_purge_blocker(session, row)
        if reason is None:
            purgeable.append(plan_id)
        else:
            blocked.append((plan_id, reason))
    return PurgePreview(selected, tuple(purgeable), tuple(blocked))


def purge_jobs(session: Session, ids: tuple[str, ...]) -> int:
    preview = preview_job_purge(session, ids)
    if preview.blocked or preview.purgeable != preview.selected:
        raise WorkspaceHistoryError("Selected jobs are not all safe to delete.")
    for job_id in preview.purgeable:
        session.delete(session.get(UiJob, job_id))
    return len(preview.purgeable)


def purge_plans(session: Session, ids: tuple[str, ...]) -> int:
    preview = preview_plan_purge(session, ids)
    if preview.blocked or preview.purgeable != preview.selected:
        raise WorkspaceHistoryError("Selected plans are not all safe to delete.")
    for plan_id in preview.purgeable:
        session.delete(session.get(ChangePlan, plan_id))
    return len(preview.purgeable)

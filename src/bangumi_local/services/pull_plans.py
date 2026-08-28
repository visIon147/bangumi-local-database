from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select

from bangumi_local.db.models import (
    BangumiSubject,
    ChangePlan,
    ChangePlanItem,
    PlanApplyRun,
    SyncConflict,
    SyncShadow,
    Work,
)
from bangumi_local.db.repositories import local_snapshot, shadow_snapshot, utc_now_iso
from bangumi_local.db.session import session_scope
from bangumi_local.domain.merge import DiffStatus, advance_base_after_pull, diff_collection
from bangumi_local.domain.models import RemoteCollection, SubjectType
from bangumi_local.domain.plans import PlanCandidate, stable_json
from bangumi_local.services.backups import backup_sqlite_database
from bangumi_local.services.plans import PlanError, StoredPlan, load_plan, save_plan
from bangumi_local.services.pull import pull_collections, snapshot_from_remote


PULL_PLAN_FORMAT = 5
PULL_PLAN_KIND = "pull"
_ABSENT_HASH = hashlib.sha256(b"bangumi-local:absent").hexdigest()


@dataclass(frozen=True, slots=True)
class PullPreflight:
    plan_id: str
    will_modify: tuple[PlanCandidate, ...]
    unchanged: tuple[PlanCandidate, ...]
    remote_by_subject: dict[int, RemoteCollection]


@dataclass(frozen=True, slots=True)
class PullApplyResult:
    run_id: str
    status: str
    applied: int
    stale: int
    failed: int
    pending: int
    backup_path: Path
    reverse_plan_id: str | None = None
    followup_tag_plan_id: str | None = None
    followup_tag_error: str | None = None


def applied_pull_subject_ids(database_url: str, plan_id: str) -> set[int]:
    with session_scope(database_url) as session:
        return set(
            session.scalars(
                select(ChangePlanItem.subject_id).where(
                    ChangePlanItem.plan_id == plan_id,
                    ChangePlanItem.item_status == "applied",
                    ChangePlanItem.subject_id.is_not(None),
                )
            ).all()
        )


def _digest(value: object) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _remote_payload(remote: RemoteCollection) -> dict[str, object]:
    subject = remote.subject
    return {
        "subject_id": remote.subject_id,
        "subject_type": int(remote.subject_type),
        "updated_at": remote.updated_at_utc,
        "collection": snapshot_from_remote(remote).as_dict(),
        "subject": {
            "title_original": subject.title_original,
            "title_cn": subject.title_cn,
            "summary": subject.summary,
            "release_date": subject.release_date,
            "cover_url": subject.cover_url,
            "metadata_available": subject.metadata_available,
            "public_tags": list(subject.public_tags),
        },
    }


def remote_pull_precondition(remote: RemoteCollection) -> str:
    return _digest(_remote_payload(remote))


def local_pull_precondition(session, subject_id: int) -> str:
    identity = session.get(BangumiSubject, subject_id)
    if identity is None:
        return _ABSENT_HASH
    work = session.get(Work, identity.work_id)
    if work is None:
        return _ABSENT_HASH
    shadow = session.get(SyncShadow, subject_id)
    conflicts = session.scalars(
        select(SyncConflict)
        .where(SyncConflict.subject_id == subject_id, SyncConflict.status == "open")
        .order_by(SyncConflict.field, SyncConflict.id)
    ).all()
    return _digest(
        {
            "identity": {
                "work_id": identity.work_id,
                "subject_type": identity.subject_type,
                "metadata_available": identity.metadata_available,
            },
            "work": {
                "kind": work.kind,
                "title": work.title,
                "title_cn": work.title_cn,
                "title_original": work.title_original,
                "summary": work.summary,
                "release_date": work.release_date,
                "cover_url": work.cover_url,
            },
            "local": local_snapshot(session, subject_id).as_dict(),
            "shadow": shadow_snapshot(shadow).as_dict() if shadow is not None else None,
            "conflicts": [
                [row.field, row.base_json, row.local_json, row.remote_json]
                for row in conflicts
            ],
        }
    )


def _metadata_changed(work: Work, remote: RemoteCollection) -> bool:
    subject = remote.subject
    if not subject.metadata_available:
        return work.kind != SubjectType(remote.subject_type).kind
    values = (
        ("kind", SubjectType(remote.subject_type).kind),
        ("title", subject.display_title),
        ("title_cn", subject.title_cn),
        ("title_original", subject.title_original),
        ("summary", subject.summary),
        ("release_date", subject.release_date),
        ("cover_url", subject.cover_url),
    )
    return any(getattr(work, key) != value for key, value in values)


def create_pull_plan(
    session,
    collections: list[RemoteCollection],
    *,
    subject_type: SubjectType | None = None,
    image_policy: str = "metadata",
) -> StoredPlan:
    if image_policy not in {"none", "metadata", "missing", "refresh"}:
        raise PlanError("Pull image policy must be none, metadata, missing, or refresh.")
    remote_by_id = {item.subject_id: item for item in collections}
    candidates: list[PlanCandidate] = []

    for subject_id in sorted(remote_by_id):
        remote = remote_by_id[subject_id]
        remote_snapshot = snapshot_from_remote(remote)
        identity = session.get(BangumiSubject, subject_id)
        evidence: dict[str, object] = {
            "base": None,
            "local": None,
            "remote": remote_snapshot.as_dict(),
            "field_statuses": {},
            "remote_record": _remote_payload(remote),
        }
        remote_hash = remote_pull_precondition(remote)
        if identity is None:
            candidates.append(
                PlanCandidate(
                    work_id=None,
                    subject_id=subject_id,
                    title=remote.subject.display_title,
                    bgm_url=f"https://bgm.tv/subject/{subject_id}",
                    disposition="planned",
                    reason="pull_import",
                    action={"operation": "pull_merge", "subject_id": subject_id},
                    selection_evidence=evidence,
                    before_snapshot=None,
                    intended_snapshot=remote_snapshot,
                    before_tags=(),
                    after_tags=remote_snapshot.tags,
                    public_tags=remote.subject.public_tags,
                    precondition_hash=remote_hash,
                    changed_fields=("metadata", "type", "rate", "comment", "private", "tags", "shadow"),
                    local_precondition_hash=_ABSENT_HASH,
                    remote_existence="present",
                )
            )
            continue

        work = session.get(Work, identity.work_id)
        if work is None:
            raise PlanError(f"Work missing for Bangumi subject {subject_id}.")
        local = local_snapshot(session, subject_id)
        shadow = session.get(SyncShadow, subject_id)
        metadata_changed = _metadata_changed(work, remote)
        evidence["local"] = local.as_dict()
        evidence["metadata_changed"] = metadata_changed
        changed_fields: list[str] = []
        intended = local
        reason = "pull_unchanged"
        disposition = "unchanged"

        if shadow is None:
            evidence["field_statuses"] = {
                field: "bootstrap_missing" for field in ("type", "rate", "comment", "private", "tags")
            }
            if local == remote_snapshot:
                changed_fields.append("shadow")
                disposition, reason = "planned", "pull_bootstrap"
            else:
                reason = "pull_bootstrap_mismatch"
        else:
            base = shadow_snapshot(shadow)
            diff = diff_collection(base, local, remote_snapshot)
            evidence["base"] = base.as_dict()
            evidence["field_statuses"] = {
                field.field: field.status.value for field in diff.fields
            }
            remote_fields = diff.fields_with_status(DiffStatus.REMOTE_CHANGED)
            conflict_fields = diff.fields_with_status(DiffStatus.CONFLICT)
            next_base = advance_base_after_pull(diff)
            if remote_fields:
                changed_fields.extend(remote_fields)
                intended = local.replacing(
                    {field: remote_snapshot.value_for(field) for field in remote_fields}
                )
            if next_base != base:
                changed_fields.append("shadow")
            if conflict_fields:
                changed_fields.append("conflicts")
            if remote_fields:
                reason = "pull_remote_update"
            elif conflict_fields:
                reason = "pull_conflict_record"
            elif next_base != base:
                reason = "pull_converged"
            elif diff.status == DiffStatus.LOCAL_CHANGED:
                reason = "pull_local_preserved"
            if changed_fields:
                disposition = "planned"

        if metadata_changed:
            changed_fields.insert(0, "metadata")
            disposition = "planned"
            if reason in {"pull_unchanged", "pull_local_preserved"}:
                reason = "pull_metadata_update"

        candidates.append(
            PlanCandidate(
                work_id=work.id,
                subject_id=subject_id,
                title=work.title,
                bgm_url=identity.url,
                disposition=disposition,
                reason=reason,
                action={"operation": "pull_merge", "subject_id": subject_id},
                selection_evidence=evidence,
                before_snapshot=local,
                intended_snapshot=intended if disposition == "planned" else None,
                before_tags=local.tags,
                after_tags=intended.tags if disposition == "planned" else None,
                public_tags=remote.subject.public_tags,
                precondition_hash=remote_hash if disposition == "planned" else None,
                changed_fields=tuple(dict.fromkeys(changed_fields)),
                local_precondition_hash=local_pull_precondition(session, subject_id),
                remote_existence="present",
            )
        )

    statement = select(BangumiSubject, Work).join(Work, Work.id == BangumiSubject.work_id)
    if subject_type is not None:
        statement = statement.where(BangumiSubject.subject_type == int(subject_type))
    for identity, work in session.execute(statement).all():
        if identity.subject_id in remote_by_id or session.get(SyncShadow, identity.subject_id) is None:
            continue
        local = local_snapshot(session, identity.subject_id)
        candidates.append(
            PlanCandidate(
                work_id=work.id,
                subject_id=identity.subject_id,
                title=work.title,
                bgm_url=identity.url,
                disposition="unchanged",
                reason="pull_remote_missing",
                action={"operation": "none", "subject_id": identity.subject_id},
                selection_evidence={
                    "base": shadow_snapshot(session.get(SyncShadow, identity.subject_id)).as_dict(),
                    "local": local.as_dict(),
                    "remote": None,
                    "field_statuses": {},
                },
                before_snapshot=local,
                before_tags=local.tags,
                changed_fields=(),
                local_precondition_hash=local_pull_precondition(session, identity.subject_id),
                remote_existence="absent",
            )
        )

    selector: dict[str, object] = {
        "mode": "remote_collections",
        "subject_type": int(subject_type) if subject_type is not None else None,
        "image_policy": image_policy,
    }
    return save_plan(
        session,
        kind=PULL_PLAN_KIND,
        operation="merge_local",
        selector=selector,
        candidates=candidates,
        format_version=PULL_PLAN_FORMAT,
    )


def preflight_pull_plan(
    database_url: str,
    plan_id: str,
    collections: list[RemoteCollection],
) -> PullPreflight:
    remote_by_id = {item.subject_id: item for item in collections}
    will_modify: list[PlanCandidate] = []
    unchanged: list[PlanCandidate] = []
    with session_scope(database_url) as session:
        stored = load_plan(session, plan_id)
        if stored.plan.kind != PULL_PLAN_KIND or stored.plan.format_version != PULL_PLAN_FORMAT:
            raise PlanError("Plan is not a v5 local pull plan.")
        for candidate in stored.candidates:
            if candidate.disposition != "planned":
                unchanged.append(candidate)
                continue
            if candidate.subject_id is None:
                unchanged.append(replace(candidate, disposition="unchanged", reason="stale_remote"))
                continue
            fresh = remote_by_id.get(candidate.subject_id)
            if fresh is None or remote_pull_precondition(fresh) != candidate.precondition_hash:
                unchanged.append(replace(candidate, disposition="unchanged", reason="stale_remote"))
                continue
            if local_pull_precondition(session, candidate.subject_id) != candidate.local_precondition_hash:
                unchanged.append(replace(candidate, disposition="unchanged", reason="stale_local"))
                continue
            will_modify.append(candidate)
    return PullPreflight(plan_id, tuple(will_modify), tuple(unchanged), remote_by_id)


def apply_pull_plan(
    database_url: str,
    plan_id: str,
    preflight: PullPreflight,
    *,
    backup_directory: Path,
) -> PullApplyResult:
    if preflight.plan_id != plan_id:
        raise PlanError("Preflight result belongs to another plan.")
    if not preflight.will_modify:
        raise PlanError("Preflight found no safe local pull items.")
    with session_scope(database_url) as session:
        stored = load_plan(session, plan_id)
        if stored.plan.kind != PULL_PLAN_KIND or stored.plan.status not in {"reviewed", "partial"}:
            raise PlanError(f"Pull plan is not applyable in status {stored.plan.status}.")

    backup_path = backup_sqlite_database(
        database_url, backup_directory, label=f"before-pull-{plan_id}"
    )
    run_id = str(uuid4())
    with session_scope(database_url) as session:
        plan = session.get(ChangePlan, plan_id)
        assert plan is not None
        plan.status = "applying"
        session.add(
            PlanApplyRun(
                id=run_id,
                plan_id=plan_id,
                status="running",
                backup_path=str(backup_path),
                started_at=utc_now_iso(),
            )
        )
        rows = {
            row.subject_id: row
            for row in session.scalars(
                select(ChangePlanItem).where(ChangePlanItem.plan_id == plan_id)
            ).all()
        }
        fresh_ids = {item.subject_id for item in preflight.will_modify}
        for item in stored.planned:
            if item.subject_id not in fresh_ids and item.subject_id in rows:
                rows[item.subject_id].item_status = "stale"
                rows[item.subject_id].error = "fresh preflight excluded this item"

    applied = failed = 0
    for candidate in preflight.will_modify:
        assert candidate.subject_id is not None
        remote = preflight.remote_by_subject[candidate.subject_id]
        try:
            with session_scope(database_url) as session:
                row = session.scalar(
                    select(ChangePlanItem).where(
                        ChangePlanItem.plan_id == plan_id,
                        ChangePlanItem.subject_id == candidate.subject_id,
                    )
                )
                assert row is not None
                if local_pull_precondition(session, candidate.subject_id) != candidate.local_precondition_hash:
                    row.item_status = "stale"
                    row.error = "stale_local"
                    continue
                pull_collections(session, [remote], scope_subject_type=remote.subject_type)
                row.item_status = "applied"
                row.error = None
                applied += 1
        except Exception as exc:  # item transaction is intentionally isolated
            failed += 1
            with session_scope(database_url) as session:
                row = session.scalar(
                    select(ChangePlanItem).where(
                        ChangePlanItem.plan_id == plan_id,
                        ChangePlanItem.subject_id == candidate.subject_id,
                    )
                )
                if row is not None:
                    row.item_status = "failed"
                    row.error = f"{type(exc).__name__}: local pull item failed"

    with session_scope(database_url) as session:
        stale = int(
            session.scalar(
                select(func.count())
                .select_from(ChangePlanItem)
                .where(
                    ChangePlanItem.plan_id == plan_id,
                    ChangePlanItem.item_status == "stale",
                )
            )
            or 0
        )
        pending = int(
            session.scalar(
                select(func.count())
                .select_from(ChangePlanItem)
                .where(
                    ChangePlanItem.plan_id == plan_id,
                    ChangePlanItem.item_status == "pending",
                )
            )
            or 0
        )
        status = "applied" if failed == 0 and stale == 0 and pending == 0 else "partial"
        plan = session.get(ChangePlan, plan_id)
        run = session.get(PlanApplyRun, run_id)
        assert plan is not None and run is not None
        plan.status = status
        plan.applied_at = utc_now_iso() if applied else None
        run.status = status
        run.finished_at = utc_now_iso()
    return PullApplyResult(
        run_id, status, applied, stale, failed, pending, backup_path
    )

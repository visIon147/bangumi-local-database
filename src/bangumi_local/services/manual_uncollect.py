from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from bangumi_local.adapters.bangumi import BangumiAPIError, BangumiClient
from bangumi_local.db.models import (
    BangumiCollectionState,
    ChangePlan,
    ChangePlanItem,
    PlanApplyRun,
    RemoteOperation,
    SyncConflict,
    SyncShadow,
)
from bangumi_local.db.repositories import local_snapshot, utc_now_iso
from bangumi_local.db.session import session_scope
from bangumi_local.domain.plans import PlanCandidate
from bangumi_local.domain.snapshots import CANONICAL_FIELDS
from bangumi_local.services.backups import BackupError, backup_sqlite_database
from bangumi_local.services.plans import PlanError, load_plan, persist_reverse_plan
from bangumi_local.services.steam_plans import absent_local_precondition


@dataclass(frozen=True, slots=True)
class ManualUncollectResult:
    plan_id: str
    run_id: str
    reconciled: int
    backup_path: Path
    restore_plan_id: str | None


@dataclass(frozen=True, slots=True)
class ManualUncollectPreflight:
    plan_id: str
    subjects: tuple[tuple[int, str], ...]


def _verify_remote_absent(client: BangumiClient, subject_id: int) -> None:
    try:
        client.get_collection(subject_id)
    except BangumiAPIError as exc:
        if exc.status_code == 404:
            return
        raise PlanError(
            f"Could not verify manual uncollect for subject {subject_id}: {exc}"
        ) from exc
    raise PlanError(
        f"Bangumi subject {subject_id} is still collected. Uncollect it on the "
        "Bangumi website before reconciliation."
    )


def _manual_candidates(database_url: str, plan_id: str) -> tuple[PlanCandidate, ...]:
    with session_scope(database_url) as session:
        stored = load_plan(session, plan_id)
        if stored.plan.format_version < 3 or stored.plan.kind != "reverse":
            raise PlanError("Manual uncollect reconciliation requires a v3 reverse plan.")
        if stored.plan.status not in {"reviewed", "failed", "partial"}:
            raise PlanError(
                f"Manual uncollect plan is not reconcilable in status {stored.plan.status}."
            )
        candidates = stored.planned
        if not candidates:
            raise PlanError("Manual uncollect plan has no actionable items.")
        for candidate in candidates:
            operation = str(candidate.action.get("operation"))
            if operation not in {"delete_collection", "manual_uncollect"}:
                raise PlanError(
                    "Plan contains an action that is not a manual uncollect recovery."
                )
            if candidate.subject_id is None or candidate.work_id is None:
                raise PlanError("Manual uncollect item is missing its Bangumi identity.")
            if candidate.before_snapshot is None:
                raise PlanError("Manual uncollect item has no verified before snapshot.")
            state = session.get(BangumiCollectionState, candidate.subject_id)
            shadow = session.get(SyncShadow, candidate.subject_id)
            if state is None or shadow is None:
                raise PlanError(
                    f"Local collection state is already absent for subject {candidate.subject_id}."
                )
            if local_snapshot(session, candidate.subject_id).digest() != candidate.local_precondition_hash:
                raise PlanError(
                    f"Local collection state changed for subject {candidate.subject_id}."
                )
        return candidates


def preflight_manual_uncollect(
    database_url: str, client: BangumiClient, plan_id: str
) -> ManualUncollectPreflight:
    """Fresh-verify that every reverse-plan collection is absent remotely."""
    candidates = _manual_candidates(database_url, plan_id)
    subjects: list[tuple[int, str]] = []
    for candidate in candidates:
        assert candidate.subject_id is not None
        _verify_remote_absent(client, candidate.subject_id)
        subjects.append((candidate.subject_id, candidate.title))
    return ManualUncollectPreflight(plan_id=plan_id, subjects=tuple(subjects))


def reconcile_manual_uncollect(
    database_url: str,
    client: BangumiClient,
    plan_id: str,
    *,
    backup_directory: Path,
) -> ManualUncollectResult:
    """Verify a website uncollect, then align LOCAL/shadow and preserve an audit trail."""
    checked = preflight_manual_uncollect(database_url, client, plan_id)
    candidates = _manual_candidates(database_url, checked.plan_id)

    try:
        backup_path = backup_sqlite_database(
            database_url, backup_directory, label=f"before-manual-uncollect-{plan_id}"
        )
    except BackupError as exc:
        raise PlanError(str(exc)) from exc

    # Close the race between the first verification and the local reconciliation.
    for candidate in candidates:
        assert candidate.subject_id is not None
        _verify_remote_absent(client, candidate.subject_id)

    run_id = str(uuid4())
    now = utc_now_iso()
    restore_candidates: list[PlanCandidate] = []
    with session_scope(database_url) as session:
        stored = load_plan(session, plan_id)
        plan = session.get(ChangePlan, plan_id)
        assert plan is not None
        rows = {
            row.subject_id: row
            for row in session.scalars(
                select(ChangePlanItem).where(ChangePlanItem.plan_id == plan_id)
            ).all()
        }
        session.add(
            PlanApplyRun(
                id=run_id,
                plan_id=plan_id,
                status="applied",
                backup_path=str(backup_path),
                started_at=now,
                finished_at=now,
            )
        )
        for candidate in stored.planned:
            assert candidate.subject_id is not None
            assert candidate.work_id is not None
            assert candidate.before_snapshot is not None
            row = rows[candidate.subject_id]
            current = local_snapshot(session, candidate.subject_id)
            if current.digest() != candidate.local_precondition_hash:
                raise PlanError(
                    f"Local collection state changed for subject {candidate.subject_id}."
                )
            for conflict in session.scalars(
                select(SyncConflict).where(
                    SyncConflict.subject_id == candidate.subject_id
                )
            ).all():
                session.delete(conflict)
            state = session.get(BangumiCollectionState, candidate.subject_id)
            shadow = session.get(SyncShadow, candidate.subject_id)
            assert state is not None and shadow is not None
            session.delete(state)
            session.delete(shadow)
            row.item_status = "applied"
            row.error = None
            session.add(
                RemoteOperation(
                    id=str(uuid4()),
                    run_id=run_id,
                    plan_id=plan_id,
                    plan_item_id=row.id,
                    work_id=candidate.work_id,
                    subject_id=candidate.subject_id,
                    source="manual",
                    before_snapshot_json=candidate.before_snapshot.to_json(),
                    intended_snapshot_json=None,
                    actual_snapshot_json=None,
                    request_payload_json=None,
                    request_method="VERIFY",
                    remote_existed_before=False,
                    status="applied",
                    attempt_count=1,
                    http_status=404,
                    error="manual_uncollect_verified",
                    started_at=now,
                    finished_at=now,
                )
            )
            values = candidate.before_snapshot.as_dict()
            restore_candidates.append(
                PlanCandidate(
                    work_id=candidate.work_id,
                    subject_id=candidate.subject_id,
                    title=candidate.title,
                    bgm_url=candidate.bgm_url,
                    disposition="planned",
                    reason="reverse_verified_manual_uncollect",
                    action={
                        "operation": "create_collection",
                        "fields": list(CANONICAL_FIELDS),
                        "values": values,
                    },
                    selection_evidence={
                        "source_plan_id": plan_id,
                        "source_run_id": run_id,
                        "manual_uncollect_verified": True,
                    },
                    before_snapshot=None,
                    intended_snapshot=candidate.before_snapshot,
                    before_tags=(),
                    after_tags=candidate.before_snapshot.tags,
                    public_tags=candidate.public_tags,
                    precondition_hash=None,
                    changed_fields=CANONICAL_FIELDS,
                    local_precondition_hash=absent_local_precondition(),
                    remote_existence="absent",
                )
            )
        plan.status = "applied"
        plan.applied_at = now
        restore = persist_reverse_plan(
            session, source_plan=plan, candidates=restore_candidates
        )
        restore_plan_id = restore.plan.id if restore is not None else None
    return ManualUncollectResult(
        plan_id=plan_id,
        run_id=run_id,
        reconciled=len(candidates),
        backup_path=backup_path,
        restore_plan_id=restore_plan_id,
    )

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from uuid import uuid4

from sqlalchemy import select

from bangumi_local.adapters.bangumi import BangumiAPIError, BangumiClient
from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    ChangePlan,
    ChangePlanItem,
    PlanApplyRun,
    RemoteOperation,
    SyncShadow,
    Work,
)
from bangumi_local.db.repositories import (
    apply_remote_fields,
    ensure_state,
    local_snapshot,
    shadow_snapshot,
    utc_now_iso,
    write_shadow,
)
from bangumi_local.db.session import session_scope
from bangumi_local.domain.plans import PlanCandidate, stable_json
from bangumi_local.domain.mutations import CollectionPatch
from bangumi_local.domain.snapshots import CollectionSnapshot
from bangumi_local.domain.tags import apply_tag_action
from bangumi_local.services.backups import backup_sqlite_database
from bangumi_local.services.plans import (
    PlanError,
    StoredPlan,
    load_plan,
    local_tag_safety_reason,
    persist_reverse_plan,
    create_bulk_tag_plan,
)
from bangumi_local.services.pull import snapshot_from_remote
from bangumi_local.services.steam_plans import absent_local_precondition, steam_source_precondition


@dataclass(frozen=True, slots=True)
class PreflightResult:
    plan_id: str
    will_modify: tuple[PlanCandidate, ...]
    unchanged: tuple[PlanCandidate, ...]


@dataclass(frozen=True, slots=True)
class ApplyResult:
    plan_id: str
    run_id: str
    status: str
    applied: int
    stale: int
    failed: int
    pending: int
    backup_path: Path
    reverse_plan_id: str | None
    followup_tag_plan_id: str | None = None
    followup_tag_error: str | None = None


class _ItemFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        http_status: int | None = None,
        actual: CollectionSnapshot | None = None,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.http_status = http_status
        self.actual = actual
        self.uncertain = uncertain


class _BatchAbort(_ItemFailure):
    pass


def _candidate_with_reason(candidate: PlanCandidate, reason: str) -> PlanCandidate:
    return replace(candidate, disposition="unchanged", reason=reason)


def _candidate_key(candidate: PlanCandidate) -> tuple[str, int]:
    if candidate.source_entry_id is not None:
        return ("source", candidate.source_entry_id)
    if candidate.subject_id is not None:
        return ("subject", candidate.subject_id)
    raise PlanError("Plan candidate has neither source entry nor subject identity.")


def _row_key(row: ChangePlanItem) -> tuple[str, int]:
    if row.source_entry_id is not None:
        return ("source", row.source_entry_id)
    if row.subject_id is not None:
        return ("subject", row.subject_id)
    raise PlanError("Stored plan item has neither source entry nor subject identity.")


def _sorted_candidates(candidates: list[PlanCandidate]) -> tuple[PlanCandidate, ...]:
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.subject_id is None,
                item.subject_id or 0,
                item.source_entry_id or 0,
            ),
        )
    )


def preflight_plan(
    database_url: str,
    client: BangumiClient,
    plan_id: str,
) -> PreflightResult:
    with session_scope(database_url) as session:
        stored = load_plan(session, plan_id)
        if stored.plan.status not in ("reviewed", "partial"):
            raise PlanError(
                f"Plan must be reviewed or resumable partial before apply; current status is {stored.plan.status}."
            )
        rows = {
            _row_key(row): row
            for row in session.scalars(
                select(ChangePlanItem).where(ChangePlanItem.plan_id == plan_id)
            ).all()
        }

    will_modify: list[PlanCandidate] = []
    unchanged: list[PlanCandidate] = list(stored.unchanged)
    for candidate in stored.planned:
        row = rows[_candidate_key(candidate)]
        if row.item_status == "applied":
            unchanged.append(_candidate_with_reason(candidate, "already_applied"))
            continue
        if row.item_status in ("stale", "failed"):
            unchanged.append(_candidate_with_reason(candidate, f"previously_{row.item_status}"))
            continue
        with session_scope(database_url) as session:
            if candidate.work_id is None or candidate.subject_id is None:
                unchanged.append(_candidate_with_reason(candidate, "local_work_missing"))
                continue
            work = session.get(Work, candidate.work_id)
            identity = session.get(BangumiSubject, candidate.subject_id)
            if work is None or identity is None or identity.work_id != candidate.work_id:
                unchanged.append(_candidate_with_reason(candidate, "local_work_missing"))
                continue
            if stored.plan.format_version >= 3:
                if candidate.source_entry_id is not None:
                    try:
                        current_source_hash, _ = steam_source_precondition(
                            session, candidate.source_entry_id
                        )
                    except PlanError:
                        local_reason = "stale_source"
                    else:
                        local_reason = (
                            "stale_source"
                            if current_source_hash != candidate.source_precondition_hash
                            else None
                        )
                else:
                    local_reason = None
                operation = str(candidate.action.get("operation"))
                state = session.get(BangumiCollectionState, candidate.subject_id)
                shadow = session.get(SyncShadow, candidate.subject_id)
                if local_reason is None and operation == "create_collection":
                    current_hash = (
                        absent_local_precondition()
                        if state is None and shadow is None
                        else "present"
                    )
                    if current_hash != candidate.local_precondition_hash:
                        local_reason = "stale_local"
                elif local_reason is None:
                    if state is None or shadow is None:
                        local_reason = "local_work_missing"
                    elif local_snapshot(session, candidate.subject_id).digest() != candidate.local_precondition_hash:
                        local_reason = "stale_local"
            elif stored.plan.format_version == 1:
                local_reason = local_tag_safety_reason(session, work)
            else:
                current_local = local_snapshot(session, candidate.subject_id)
                local_reason = (
                    "stale_local"
                    if current_local.digest() != candidate.local_precondition_hash
                    else None
                )
        if local_reason is not None:
            unchanged.append(_candidate_with_reason(candidate, local_reason))
            continue
        try:
            fresh = client.get_collection(candidate.subject_id)
        except BangumiAPIError as exc:
            if exc.status_code in (401, 403):
                raise PlanError(str(exc)) from exc
            if stored.plan.format_version >= 3 and exc.status_code == 404:
                if candidate.remote_existence == "absent":
                    will_modify.append(candidate)
                else:
                    unchanged.append(
                        _candidate_with_reason(candidate, "stale_remote_existence")
                    )
                continue
            unchanged.append(_candidate_with_reason(candidate, "preflight_read_failed"))
            continue
        if stored.plan.format_version >= 3 and candidate.remote_existence == "absent":
            unchanged.append(_candidate_with_reason(candidate, "stale_remote_existence"))
            continue
        if snapshot_from_remote(fresh).digest() != candidate.precondition_hash:
            unchanged.append(_candidate_with_reason(candidate, "stale_remote"))
            continue
        will_modify.append(candidate)
    return PreflightResult(
        plan_id=plan_id,
        will_modify=tuple(will_modify),
        unchanged=_sorted_candidates(unchanged),
    )


def _verify_after_error(
    client: BangumiClient,
    subject_id: int,
    before: CollectionSnapshot,
    intended: CollectionSnapshot,
    attempt: int,
) -> tuple[str, CollectionSnapshot | None]:
    try:
        actual = snapshot_from_remote(client.get_collection(subject_id))
    except BangumiAPIError as exc:
        if exc.status_code in (401, 403):
            raise _BatchAbort(
                str(exc), attempts=attempt, http_status=exc.status_code
            ) from exc
        return "unavailable", None
    if actual == intended:
        return "intended", actual
    if actual == before:
        return "before", actual
    return "other", actual


def _patch_with_verification(
    client: BangumiClient,
    *,
    subject_id: int,
    before: CollectionSnapshot,
    intended: CollectionSnapshot,
    patch: CollectionPatch,
    max_retries: int,
    retry_base_seconds: float,
    sleep_fn: Callable[[float], None],
) -> tuple[CollectionSnapshot, int, int]:
    for attempt in range(1, max_retries + 2):
        try:
            client.patch_collection(subject_id, patch)
        except BangumiAPIError as exc:
            status = exc.status_code
            if status in (401, 403):
                raise _BatchAbort(str(exc), attempts=attempt, http_status=status) from exc
            retryable = exc.timed_out or status == 429 or (status is not None and status >= 500)
            if not retryable:
                raise _ItemFailure(str(exc), attempts=attempt, http_status=status) from exc
            remote_state, actual = _verify_after_error(
                client, subject_id, before, intended, attempt
            )
            if remote_state == "intended" and actual is not None:
                return actual, attempt, status or 204
            if remote_state == "other":
                raise _ItemFailure(
                    "Remote state changed to neither before nor intended after a write error.",
                    attempts=attempt,
                    http_status=status,
                    actual=actual,
                    uncertain=True,
                ) from exc
            if remote_state == "unavailable":
                raise _ItemFailure(
                    "Could not verify remote state after a write error; no blind retry was attempted.",
                    attempts=attempt,
                    http_status=status,
                    uncertain=True,
                ) from exc
            if attempt > max_retries:
                raise _ItemFailure(
                    "Bangumi write retry limit reached.",
                    attempts=attempt,
                    http_status=status,
                    actual=actual,
                ) from exc
            delay = exc.retry_after_seconds
            if delay is None:
                delay = retry_base_seconds * (2 ** (attempt - 1))
            sleep_fn(delay)
            continue

        try:
            verified = snapshot_from_remote(client.get_collection(subject_id))
        except BangumiAPIError as exc:
            if exc.status_code in (401, 403):
                raise _BatchAbort(
                    str(exc), attempts=attempt, http_status=exc.status_code
                ) from exc
            raise _ItemFailure(
                "PATCH returned success but GET verification failed.",
                attempts=attempt,
                http_status=exc.status_code or 204,
                uncertain=True,
            ) from exc
        if verified != intended:
            raise _ItemFailure(
                "GET verification did not match the intended canonical snapshot.",
                attempts=attempt,
                http_status=204,
                actual=verified,
                uncertain=True,
            )
        return verified, attempt, 204
    raise AssertionError("unreachable")


def _create_with_verification(
    client: BangumiClient,
    *,
    subject_id: int,
    intended: CollectionSnapshot,
    patch: CollectionPatch,
    max_retries: int,
    retry_base_seconds: float,
    sleep_fn: Callable[[float], None],
) -> tuple[CollectionSnapshot, int, int]:
    for attempt in range(1, max_retries + 2):
        try:
            client.create_collection(subject_id, patch)
        except BangumiAPIError as exc:
            status = exc.status_code
            if status in (401, 403):
                raise _BatchAbort(str(exc), attempts=attempt, http_status=status) from exc
            retryable = exc.timed_out or status == 429 or (status is not None and status >= 500)
            if not retryable:
                raise _ItemFailure(str(exc), attempts=attempt, http_status=status) from exc
            try:
                actual = snapshot_from_remote(client.get_collection(subject_id))
            except BangumiAPIError as read_exc:
                if read_exc.status_code in (401, 403):
                    raise _BatchAbort(
                        str(read_exc), attempts=attempt, http_status=read_exc.status_code
                    ) from read_exc
                if read_exc.status_code == 404:
                    actual = None
                else:
                    raise _ItemFailure(
                        "Could not verify collection creation after a write error.",
                        attempts=attempt,
                        http_status=status,
                        uncertain=True,
                    ) from read_exc
            if actual == intended:
                return actual, attempt, status or 204
            if actual is not None:
                raise _ItemFailure(
                    "Created collection differs from the intended canonical snapshot.",
                    attempts=attempt,
                    http_status=status,
                    actual=actual,
                    uncertain=True,
                ) from exc
            if attempt > max_retries:
                raise _ItemFailure(
                    "Bangumi collection create retry limit reached.",
                    attempts=attempt,
                    http_status=status,
                ) from exc
            delay = exc.retry_after_seconds
            if delay is None:
                delay = retry_base_seconds * (2 ** (attempt - 1))
            sleep_fn(delay)
            continue
        try:
            actual = snapshot_from_remote(client.get_collection(subject_id))
        except BangumiAPIError as exc:
            if exc.status_code in (401, 403):
                raise _BatchAbort(str(exc), attempts=attempt, http_status=exc.status_code) from exc
            raise _ItemFailure(
                "POST returned success but GET verification failed.",
                attempts=attempt,
                http_status=exc.status_code or 204,
                uncertain=True,
            ) from exc
        if actual != intended:
            raise _ItemFailure(
                "GET verification did not match the intended new collection snapshot.",
                attempts=attempt,
                http_status=204,
                actual=actual,
                uncertain=True,
            )
        return actual, attempt, 204
    raise AssertionError("unreachable")


def _mark_item(session_url: str, item_id: int, status: str, error: str | None = None) -> None:
    with session_scope(session_url) as session:
        item = session.get(ChangePlanItem, item_id)
        if item is None:
            raise PlanError(f"Plan item disappeared: {item_id}")
        item.item_status = status
        item.error = error


def _update_operation_failure(
    database_url: str,
    operation_id: str,
    failure: _ItemFailure,
) -> None:
    with session_scope(database_url) as session:
        operation = session.get(RemoteOperation, operation_id)
        if operation is None:
            raise PlanError(f"Audit operation disappeared: {operation_id}")
        operation.status = "uncertain" if failure.uncertain else "failed"
        operation.attempt_count = failure.attempts
        operation.http_status = failure.http_status
        operation.error = str(failure)
        operation.actual_snapshot_json = (
            failure.actual.to_json() if failure.actual is not None else None
        )
        operation.finished_at = utc_now_iso()


def _finalize_run(
    database_url: str,
    *,
    plan_id: str,
    run_id: str,
    aborted: bool,
    reverse_candidates: list[PlanCandidate],
) -> tuple[str, int, int, int, int, str | None]:
    with session_scope(database_url) as session:
        plan = session.get(ChangePlan, plan_id)
        run = session.get(PlanApplyRun, run_id)
        if plan is None or run is None:
            raise PlanError("Plan or apply run disappeared during finalization.")
        items = session.scalars(
            select(ChangePlanItem).where(
                ChangePlanItem.plan_id == plan_id,
                ChangePlanItem.disposition == "planned",
            )
        ).all()
        counts = {
            name: sum(item.item_status == name for item in items)
            for name in ("applied", "stale", "failed", "pending")
        }
        if counts["pending"] or counts["stale"] or counts["failed"]:
            plan.status = "partial" if counts["applied"] or counts["pending"] else "failed"
        else:
            plan.status = "applied"
            plan.applied_at = utc_now_iso()
        run.status = "aborted" if aborted else plan.status
        if run.status == "applied":
            run.status = "applied"
        elif run.status not in ("aborted", "failed"):
            run.status = "partial"
        run.finished_at = utc_now_iso()
        reverse = persist_reverse_plan(
            session, source_plan=plan, candidates=reverse_candidates
        )
        return (
            plan.status,
            counts["applied"],
            counts["stale"],
            counts["failed"],
            counts["pending"],
            reverse.plan.id if reverse is not None else None,
        )


def _create_followup_tag_plan(
    database_url: str,
    client: BangumiClient,
    *,
    source_plan_id: str,
) -> tuple[str | None, str | None]:
    with session_scope(database_url) as session:
        stored = load_plan(session, source_plan_id)
        selector = json.loads(stored.plan.selector_json)
        tag = selector.get("followup_tag") if stored.plan.format_version >= 3 else None
        if not isinstance(tag, str) or not tag:
            return None, None
        rows = {
            _row_key(row): row
            for row in session.scalars(
                select(ChangePlanItem).where(ChangePlanItem.plan_id == source_plan_id)
            ).all()
        }
        targets: list[PlanCandidate] = []
        for candidate in stored.candidates:
            if candidate.subject_id is None:
                continue
            row = rows[_candidate_key(candidate)]
            if row.item_status == "applied" or candidate.reason == "steam_status_already_desired":
                targets.append(candidate)
    remote: list = []
    target_ids: list[int] = []
    try:
        for candidate in targets:
            assert candidate.subject_id is not None
            if candidate.source_entry_id is not None:
                with session_scope(database_url) as session:
                    source_hash, _ = steam_source_precondition(
                        session, candidate.source_entry_id
                    )
                if source_hash != candidate.source_precondition_hash:
                    continue
            item = client.get_collection(candidate.subject_id)
            intended = candidate.intended_snapshot
            if intended is not None and int(item.status) != intended.collection_type:
                continue
            remote.append(item)
            target_ids.append(candidate.subject_id)
        if not target_ids:
            return None, "No fresh-verified Steam status targets were eligible for the Tag draft."
        with session_scope(database_url) as session:
            followup = create_bulk_tag_plan(
                session,
                remote,
                operation="add",
                selector={"mode": "ids", "ids": target_ids},
                tag=tag,
                detail_loader=client.get_subject_public_tags,
            )
            followup.plan.created_by = "steam"
            followup.plan.selector_json = stable_json(
                {
                    "mode": "ids",
                    "ids": target_ids,
                    "source_steam_status_plan_id": source_plan_id,
                }
            )
            # Selector participates in the immutable hash; regenerate through the domain model.
            from bangumi_local.domain.plans import content_digest, plan_content

            followup.plan.content_hash = content_digest(
                plan_content(
                    kind=followup.plan.kind,
                    operation=followup.plan.operation,
                    tag=followup.plan.tag,
                    old_tag=followup.plan.old_tag,
                    new_tag=followup.plan.new_tag,
                    selector=json.loads(followup.plan.selector_json),
                    candidates=list(followup.candidates),
                    reverse_of_plan_id=followup.plan.reverse_of_plan_id,
                    format_version=followup.plan.format_version,
                )
            )
            return followup.plan.id, None
    except (BangumiAPIError, PlanError, ValueError) as exc:
        return None, str(exc)


def apply_reviewed_plan(
    database_url: str,
    client: BangumiClient,
    plan_id: str,
    preflight: PreflightResult,
    *,
    backup_directory: Path,
    write_delay_seconds: float,
    max_retries: int,
    retry_base_seconds: float,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> ApplyResult:
    if preflight.plan_id != plan_id:
        raise PlanError("Preflight result belongs to a different plan.")
    if not preflight.will_modify:
        raise PlanError("Preflight found no items that are safe to modify.")
    with session_scope(database_url) as session:
        stored: StoredPlan = load_plan(session, plan_id)
        if stored.plan.status not in ("reviewed", "partial"):
            raise PlanError(f"Plan is not applyable in status {stored.plan.status}.")
        if any(
            str(candidate.action.get("operation"))
            in {"delete_collection", "manual_uncollect"}
            for candidate in preflight.will_modify
        ):
            raise PlanError(
                "Bangumi public v0 has no collection DELETE endpoint. Uncollect the "
                "subject on the Bangumi website, then run 'bld plan "
                "reconcile-manual-uncollect PLAN_ID'."
            )

    backup_path = backup_sqlite_database(database_url, backup_directory, label=f"before-{plan_id}")
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
            _row_key(row): row.id
            for row in session.scalars(
                select(ChangePlanItem).where(ChangePlanItem.plan_id == plan_id)
            ).all()
        }

    planned_keys = {_candidate_key(candidate) for candidate in stored.planned}
    stale_reasons = {
        "stale_remote",
        "stale_local",
        "local_work_missing",
        "local_tags_changed",
        "local_tag_conflict",
        "missing_shadow",
        "stale_source",
        "stale_remote_existence",
    }
    for excluded in preflight.unchanged:
        if (
            _candidate_key(excluded) in planned_keys
            and excluded.reason in stale_reasons
        ):
            _mark_item(
                database_url,
                rows[_candidate_key(excluded)],
                "stale",
                excluded.reason,
            )

    reverse_candidates: list[PlanCandidate] = []
    aborted = False
    for index, candidate in enumerate(preflight.will_modify):
        item_id = rows[_candidate_key(candidate)]
        if candidate.work_id is None or candidate.subject_id is None:
            _mark_item(database_url, item_id, "stale", "local_work_missing")
            continue
        with session_scope(database_url) as session:
            work = session.get(Work, candidate.work_id)
            identity = session.get(BangumiSubject, candidate.subject_id)
            if work is None or identity is None or identity.work_id != candidate.work_id:
                local_reason = "local_work_missing"
            elif stored.plan.format_version >= 3:
                local_reason = None
                if candidate.source_entry_id is not None:
                    try:
                        current_source_hash, _ = steam_source_precondition(
                            session, candidate.source_entry_id
                        )
                    except PlanError:
                        local_reason = "stale_source"
                    else:
                        if current_source_hash != candidate.source_precondition_hash:
                            local_reason = "stale_source"
                action_operation = str(candidate.action.get("operation"))
                state = session.get(BangumiCollectionState, candidate.subject_id)
                shadow = session.get(SyncShadow, candidate.subject_id)
                if local_reason is None and action_operation == "create_collection":
                    current_hash = (
                        absent_local_precondition()
                        if state is None and shadow is None
                        else "present"
                    )
                    if current_hash != candidate.local_precondition_hash:
                        local_reason = "stale_local"
                elif local_reason is None:
                    if state is None or shadow is None:
                        local_reason = "local_work_missing"
                    elif local_snapshot(session, candidate.subject_id).digest() != candidate.local_precondition_hash:
                        local_reason = "stale_local"
            elif stored.plan.format_version == 1:
                local_reason = local_tag_safety_reason(session, work)
            else:
                current_local = local_snapshot(session, candidate.subject_id)
                local_reason = (
                    "stale_local"
                    if current_local.digest() != candidate.local_precondition_hash
                    else None
                )
        if local_reason is not None:
            _mark_item(database_url, item_id, "stale", local_reason)
            continue
        action = candidate.action
        action_operation = str(action.get("operation"))
        fresh_remote = None
        fresh_before: CollectionSnapshot | None = None
        try:
            fresh_remote = client.get_collection(candidate.subject_id)
            fresh_before = snapshot_from_remote(fresh_remote)
        except BangumiAPIError as exc:
            if exc.status_code in (401, 403):
                aborted = True
                break
            if not (
                stored.plan.format_version >= 3
                and action_operation == "create_collection"
                and exc.status_code == 404
            ):
                _mark_item(database_url, item_id, "failed", "fresh_read_failed")
                continue
        if action_operation == "create_collection":
            if fresh_before is not None:
                _mark_item(database_url, item_id, "stale", "stale_remote_existence")
                continue
        elif fresh_before is None or fresh_remote is None:
            _mark_item(database_url, item_id, "stale", "stale_remote_existence")
            continue
        elif fresh_before.digest() != candidate.precondition_hash:
            _mark_item(database_url, item_id, "stale", "stale_remote")
            continue

        patch: CollectionPatch | None
        intended: CollectionSnapshot | None
        if stored.plan.format_version == 1:
            assert fresh_remote is not None and fresh_before is not None
            intended_tags = apply_tag_action(
                fresh_remote.tags,
                str(action["operation"]),
                tag=action.get("tag"),
                old_tag=action.get("old_tag"),
                new_tag=action.get("new_tag"),
                set_tags=action.get("set_tags"),
            )
            patch = CollectionPatch({"tags": intended_tags})
            changed_fields = ("tags",)
            intended = patch.apply_to(fresh_before)
            request_method = "PATCH"
        elif stored.plan.format_version >= 3:
            changed_fields = candidate.changed_fields
            if action_operation == "create_collection":
                patch = CollectionPatch(action.get("values", {}))
                intended = candidate.intended_snapshot
                if intended is None:
                    raise PlanError("Create plan item has no intended snapshot.")
                request_method = "POST"
            elif action_operation == "patch_collection":
                assert fresh_before is not None
                patch = CollectionPatch(action.get("values", {}))
                intended = patch.apply_to(fresh_before)
                request_method = "PATCH"
            else:
                raise PlanError(f"Unsupported v3 plan action: {action_operation}")
        else:
            assert fresh_before is not None
            patch = CollectionPatch(action.get("values", {}))
            changed_fields = candidate.changed_fields
            intended = patch.apply_to(fresh_before)
            request_method = "PATCH"
        operation_id = str(uuid4())
        with session_scope(database_url) as session:
            session.add(
                RemoteOperation(
                    id=operation_id,
                    run_id=run_id,
                    plan_id=plan_id,
                    plan_item_id=item_id,
                    work_id=candidate.work_id,
                    subject_id=candidate.subject_id,
                    source=(
                        "bulk_tag"
                        if stored.plan.format_version == 1
                        else ("steam" if stored.plan.format_version >= 3 else "sync")
                    ),
                    before_snapshot_json=(
                        fresh_before.to_json() if fresh_before is not None else None
                    ),
                    intended_snapshot_json=(
                        intended.to_json() if intended is not None else None
                    ),
                    request_payload_json=(
                        stable_json(patch.as_api_payload()) if patch is not None else None
                    ),
                    request_method=request_method,
                    remote_existed_before=fresh_before is not None,
                    status="started",
                    attempt_count=0,
                    started_at=utc_now_iso(),
                )
            )

        try:
            if action_operation == "create_collection":
                assert patch is not None and intended is not None
                actual, attempts, http_status = _create_with_verification(
                    client,
                    subject_id=candidate.subject_id,
                    intended=intended,
                    patch=patch,
                    max_retries=max_retries,
                    retry_base_seconds=retry_base_seconds,
                    sleep_fn=sleep_fn,
                )
            else:
                assert fresh_before is not None and intended is not None and patch is not None
                actual, attempts, http_status = _patch_with_verification(
                    client,
                    subject_id=candidate.subject_id,
                    before=fresh_before,
                    intended=intended,
                    patch=patch,
                    max_retries=max_retries,
                    retry_base_seconds=retry_base_seconds,
                    sleep_fn=sleep_fn,
                )
        except _BatchAbort as failure:
            _update_operation_failure(database_url, operation_id, failure)
            _mark_item(database_url, item_id, "failed", str(failure))
            aborted = True
            break
        except _ItemFailure as failure:
            _update_operation_failure(database_url, operation_id, failure)
            _mark_item(database_url, item_id, "failed", str(failure))
            if index < len(preflight.will_modify) - 1 and write_delay_seconds > 0:
                sleep_fn(write_delay_seconds)
            continue

        with session_scope(database_url) as session:
            operation = session.get(RemoteOperation, operation_id)
            item = session.get(ChangePlanItem, item_id)
            shadow = session.get(SyncShadow, candidate.subject_id)
            if operation is None or item is None:
                raise PlanError("Local state disappeared after verified remote write.")
            if action_operation == "create_collection":
                assert actual is not None
                ensure_state(session, candidate.subject_id)
                apply_remote_fields(
                    session,
                    candidate.subject_id,
                    actual,
                    ("type", "rate", "comment", "private", "tags"),
                )
                write_shadow(session, candidate.subject_id, actual, utc_now_iso())
            else:
                assert actual is not None and fresh_remote is not None and shadow is not None
                apply_remote_fields(session, candidate.subject_id, actual, changed_fields)
                next_base = shadow_snapshot(shadow).replacing(
                    {field: actual.value_for(field) for field in changed_fields}
                )
                write_shadow(
                    session,
                    candidate.subject_id,
                    next_base,
                    fresh_remote.updated_at_utc,
                )
            item.item_status = "applied"
            item.error = None
            operation.status = "applied"
            operation.attempt_count = attempts
            operation.http_status = http_status
            operation.actual_snapshot_json = actual.to_json() if actual is not None else None
            operation.finished_at = utc_now_iso()

        if action_operation == "create_collection":
            assert actual is not None
            reverse_action = {"operation": "manual_uncollect", "fields": [], "values": {}}
            reverse_intended = None
            reverse_before = actual
            reverse_precondition = actual.digest()
            reverse_local_precondition = actual.digest()
            reverse_remote_existence = "present"
        elif stored.plan.format_version == 1:
            assert actual is not None
            reverse_values: dict[str, object] = {"tags": candidate.before_tags}
            reverse_action = {"operation": "set", "set_tags": list(candidate.before_tags)}
            reverse_intended = actual.replacing(reverse_values)
            reverse_before = actual
            reverse_precondition = actual.digest()
            reverse_local_precondition = None
            reverse_remote_existence = None
        else:
            assert fresh_before is not None and actual is not None
            reverse_values = {
                field: fresh_before.value_for(field) for field in changed_fields
            }
            reverse_action = {
                "operation": (
                    "patch_collection" if stored.plan.format_version >= 3 else "patch"
                ),
                "fields": list(changed_fields),
                "values": reverse_values,
            }
            reverse_intended = actual.replacing(reverse_values)
            reverse_before = actual
            reverse_precondition = actual.digest()
            reverse_local_precondition = actual.digest()
            reverse_remote_existence = "present" if stored.plan.format_version >= 3 else None
        verified_tag_order = (
            tuple(patch.values["tags"])
            if patch is not None and "tags" in patch.values
            else (actual.tags if actual is not None else ())
        )
        reverse_candidates.append(
            PlanCandidate(
                work_id=candidate.work_id,
                subject_id=candidate.subject_id,
                title=candidate.title,
                bgm_url=candidate.bgm_url,
                disposition="planned",
                reason="reverse_verified_write",
                action=reverse_action,
                selection_evidence={"source_plan_id": plan_id, "source_run_id": run_id},
                before_snapshot=reverse_before,
                intended_snapshot=reverse_intended,
                before_tags=verified_tag_order,
                after_tags=reverse_intended.tags if reverse_intended is not None else None,
                public_tags=candidate.public_tags,
                precondition_hash=reverse_precondition,
                changed_fields=changed_fields if stored.plan.format_version >= 2 else (),
                local_precondition_hash=(
                    reverse_local_precondition if stored.plan.format_version >= 2 else None
                ),
                source_entry_id=None,
                source_precondition_hash=None,
                remote_existence=reverse_remote_existence,
            )
        )
        if index < len(preflight.will_modify) - 1 and write_delay_seconds > 0:
            sleep_fn(write_delay_seconds)

    status, applied, stale, failed, pending, reverse_plan_id = _finalize_run(
        database_url,
        plan_id=plan_id,
        run_id=run_id,
        aborted=aborted,
        reverse_candidates=reverse_candidates,
    )
    followup_tag_plan_id, followup_tag_error = _create_followup_tag_plan(
        database_url,
        client,
        source_plan_id=plan_id,
    )
    return ApplyResult(
        plan_id=plan_id,
        run_id=run_id,
        status=status,
        applied=applied,
        stale=stale,
        failed=failed,
        pending=pending,
        backup_path=backup_path,
        reverse_plan_id=reverse_plan_id,
        followup_tag_plan_id=followup_tag_plan_id,
        followup_tag_error=followup_tag_error,
    )

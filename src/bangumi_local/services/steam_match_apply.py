from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select

from bangumi_local.adapters.bangumi import BangumiAPIError, BangumiClient
from bangumi_local.db.models import (
    ChangePlan,
    ChangePlanItem,
    LibraryEntry,
    PlanApplyRun,
    SourceAccount,
)
from bangumi_local.db.repositories import utc_now_iso
from bangumi_local.db.session import session_scope
from bangumi_local.domain.models import SubjectType
from bangumi_local.domain.plans import PlanCandidate
from bangumi_local.services.apply_plan import ApplyResult, PreflightResult
from bangumi_local.services.backups import backup_sqlite_database
from bangumi_local.services.plans import PlanError, load_plan
from bangumi_local.services.steam_matching import (
    SteamMatchError,
    confirm_match_subject,
    set_match_disposition,
)
from bangumi_local.services.steam_match_plans import (
    steam_match_source_precondition,
    target_has_other_steam_link,
)


def _excluded(candidate: PlanCandidate, reason: str) -> PlanCandidate:
    return replace(candidate, disposition="unchanged", reason=reason)


def preflight_steam_match_plan(
    database_url: str, client: BangumiClient, plan_id: str
) -> PreflightResult:
    with session_scope(database_url) as session:
        stored = load_plan(session, plan_id)
        if stored.plan.kind != "steam_match" or stored.plan.format_version < 4:
            raise PlanError("Plan is not a v4 Steam match plan.")
        if stored.plan.status not in {"reviewed", "partial"}:
            raise PlanError(
                f"Plan must be reviewed or resumable partial; current status is {stored.plan.status}."
            )
        rows = {
            row.source_entry_id: row
            for row in session.scalars(
                select(ChangePlanItem).where(ChangePlanItem.plan_id == plan_id)
            ).all()
            if row.source_entry_id is not None
        }
        will_modify: list[PlanCandidate] = []
        unchanged: list[PlanCandidate] = list(stored.unchanged)
        for candidate in stored.planned:
            if candidate.source_entry_id is None:
                unchanged.append(_excluded(candidate, "stale_source"))
                continue
            row = rows.get(candidate.source_entry_id)
            if row is None:
                unchanged.append(_excluded(candidate, "stale_source"))
                continue
            if row.item_status == "applied":
                unchanged.append(_excluded(candidate, "already_applied"))
                continue
            if row.item_status == "stale":
                unchanged.append(_excluded(candidate, "previously_stale"))
                continue
            if row.item_status == "failed":
                unchanged.append(_excluded(candidate, "previously_failed"))
                continue
            try:
                current_hash, _ = steam_match_source_precondition(
                    session, candidate.source_entry_id
                )
            except PlanError:
                unchanged.append(_excluded(candidate, "stale_source"))
                continue
            if current_hash != candidate.source_precondition_hash:
                unchanged.append(_excluded(candidate, "stale_source"))
                continue
            operation = str(candidate.action.get("operation"))
            if operation == "confirm_match":
                subject_id = candidate.action.get("subject_id")
                if not isinstance(subject_id, int) or subject_id != candidate.subject_id:
                    raise PlanError("Steam match plan contains an invalid subject action.")
                try:
                    subject = client.get_subject(subject_id)
                except BangumiAPIError as exc:
                    if exc.status_code in {401, 403}:
                        raise
                    unchanged.append(_excluded(candidate, "steam_match_subject_read_failed"))
                    continue
                if subject.subject_type != SubjectType.GAME:
                    unchanged.append(_excluded(candidate, "steam_match_subject_not_game"))
                    continue
                entry = session.get(LibraryEntry, candidate.source_entry_id)
                if entry is None or target_has_other_steam_link(
                    session, subject_id=subject_id, app_id=entry.external_id
                ):
                    unchanged.append(_excluded(candidate, "steam_match_mapping_collision"))
                    continue
            elif operation not in {"set_no_subject", "defer_match"}:
                raise PlanError(f"Unsupported Steam match action: {operation}")
            will_modify.append(candidate)
    return PreflightResult(
        plan_id=plan_id,
        will_modify=tuple(sorted(will_modify, key=lambda item: item.source_entry_id or 0)),
        unchanged=tuple(sorted(unchanged, key=lambda item: item.source_entry_id or 0)),
    )


def _mark_item(
    database_url: str, *, item_id: int, status: str, error: str | None
) -> None:
    with session_scope(database_url) as session:
        item = session.get(ChangePlanItem, item_id)
        if item is None:
            raise PlanError("Steam match plan item disappeared during apply.")
        item.item_status = status
        item.error = error


def apply_steam_match_plan(
    database_url: str,
    client: BangumiClient,
    plan_id: str,
    preflight: PreflightResult,
    *,
    backup_directory: Path,
) -> ApplyResult:
    if preflight.plan_id != plan_id or not preflight.will_modify:
        raise PlanError("A non-empty fresh preflight is required for Steam match apply.")
    with session_scope(database_url) as session:
        stored = load_plan(session, plan_id)
        if stored.plan.kind != "steam_match" or stored.plan.format_version < 4:
            raise PlanError("Plan is not a v4 Steam match plan.")
        if stored.plan.status not in {"reviewed", "partial"}:
            raise PlanError(f"Plan is not applyable in status {stored.plan.status}.")

    backup_path = backup_sqlite_database(
        database_url, backup_directory, label=f"before-{plan_id}"
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
        row_ids = {
            row.source_entry_id: row.id
            for row in session.scalars(
                select(ChangePlanItem).where(ChangePlanItem.plan_id == plan_id)
            ).all()
            if row.source_entry_id is not None
        }

    planned_source_ids = {
        item.source_entry_id for item in stored.planned if item.source_entry_id is not None
    }
    for item in preflight.unchanged:
        if item.source_entry_id in planned_source_ids and item.reason.startswith("stale_"):
            _mark_item(
                database_url,
                item_id=row_ids[item.source_entry_id],
                status="stale",
                error=item.reason,
            )

    aborted = False
    for candidate in preflight.will_modify:
        assert candidate.source_entry_id is not None
        item_id = row_ids[candidate.source_entry_id]
        try:
            operation = str(candidate.action.get("operation"))
            fresh_subject = None
            if operation == "confirm_match":
                fresh_subject = client.get_subject(int(candidate.action["subject_id"]))
                if fresh_subject.subject_type != SubjectType.GAME:
                    raise SteamMatchError("Steam match target is no longer a game subject.")
            with session_scope(database_url) as session:
                current_hash, _ = steam_match_source_precondition(
                    session, candidate.source_entry_id
                )
                if current_hash != candidate.source_precondition_hash:
                    raise PlanError("stale_source")
                entry = session.get(LibraryEntry, candidate.source_entry_id)
                if entry is None:
                    raise PlanError("stale_source")
                account = session.get(SourceAccount, entry.source_account_id)
                if account is None:
                    raise PlanError("stale_source")
                evidence = {
                    "plan_item_source_entry_id": candidate.source_entry_id,
                    "review_mode": candidate.selection_evidence.get("review_mode"),
                    "top_margin": candidate.selection_evidence.get("top_margin"),
                    "auto_checks": candidate.selection_evidence.get("auto_checks"),
                }
                if operation == "confirm_match":
                    score_value = candidate.action.get("score")
                    score = int(score_value) if isinstance(score_value, int) else None
                    confirmation = str(candidate.action.get("confirmation"))
                    automatic = confirmation == "automatic"
                    assert fresh_subject is not None
                    confirm_match_subject(
                        session,
                        candidate=fresh_subject,
                        app_id=entry.external_id,
                        account_id=account.external_account_id,
                        match_source=(
                            "automatic_match_plan" if automatic else "manual_match_plan"
                        ),
                        match_confidence="high" if automatic else "confirmed",
                        review_reason=(
                            "automatic_threshold_confirmation"
                            if automatic
                            else "manual_plan_confirmation"
                        ),
                        plan_id=plan_id,
                        score=score,
                        evidence=evidence,
                    )
                elif operation == "set_no_subject":
                    set_match_disposition(
                        session,
                        app_id=entry.external_id,
                        account_id=account.external_account_id,
                        decision="no_subject",
                        reason="manual_plan_no_subject",
                        plan_id=plan_id,
                        evidence=evidence,
                    )
                elif operation == "defer_match":
                    set_match_disposition(
                        session,
                        app_id=entry.external_id,
                        account_id=account.external_account_id,
                        decision="deferred",
                        reason="manual_plan_deferred",
                        plan_id=plan_id,
                        evidence=evidence,
                    )
                else:
                    raise PlanError(f"Unsupported Steam match action: {operation}")
            _mark_item(database_url, item_id=item_id, status="applied", error=None)
        except BangumiAPIError as exc:
            _mark_item(database_url, item_id=item_id, status="failed", error=str(exc))
            if exc.status_code in {401, 403}:
                aborted = True
                break
        except (PlanError, SteamMatchError) as exc:
            status = "stale" if str(exc) == "stale_source" else "failed"
            _mark_item(database_url, item_id=item_id, status=status, error=str(exc))

    with session_scope(database_url) as session:
        counts = dict(
            session.execute(
                select(ChangePlanItem.item_status, func.count(ChangePlanItem.id))
                .where(ChangePlanItem.plan_id == plan_id)
                .group_by(ChangePlanItem.item_status)
            ).all()
        )
        applied = int(counts.get("applied", 0))
        stale = int(counts.get("stale", 0))
        failed = int(counts.get("failed", 0))
        pending = int(counts.get("pending", 0))
        if aborted:
            status = "partial" if applied else "failed"
            run_status = "aborted"
        elif pending or stale or failed:
            status = "partial" if applied else "failed"
            run_status = status
        else:
            status = "applied"
            run_status = "applied"
        plan = session.get(ChangePlan, plan_id)
        run = session.get(PlanApplyRun, run_id)
        assert plan is not None and run is not None
        plan.status = status
        plan.applied_at = utc_now_iso() if applied else None
        run.status = run_status
        run.finished_at = utc_now_iso()
        run.error = "authorization_failed" if aborted else None
    return ApplyResult(
        plan_id=plan_id,
        run_id=run_id,
        status=status,
        applied=applied,
        stale=stale,
        failed=failed,
        pending=pending,
        backup_path=backup_path,
        reverse_plan_id=None,
    )

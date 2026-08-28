from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from bangumi_local.adapters.bangumi import BangumiAPIError
from bangumi_local.db.models import (
    BangumiSubject,
    ChangePlan,
    ChangePlanItem,
    RemoteOperation,
    SyncConflict,
    SyncShadow,
    Work,
)
from bangumi_local.db.repositories import local_snapshot, shadow_snapshot, utc_now_iso
from bangumi_local.domain.models import RemoteCollection, SubjectType
from bangumi_local.domain.plans import PlanCandidate, content_digest, plan_content, stable_json
from bangumi_local.domain.snapshots import CANONICAL_FIELDS, CollectionSnapshot
from bangumi_local.domain.tags import apply_tag_action, validate_tag
from bangumi_local.services.pull import snapshot_from_remote


class PlanError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StoredPlan:
    plan: ChangePlan
    candidates: tuple[PlanCandidate, ...]

    @property
    def planned(self) -> tuple[PlanCandidate, ...]:
        return tuple(item for item in self.candidates if item.disposition == "planned")

    @property
    def unchanged(self) -> tuple[PlanCandidate, ...]:
        return tuple(item for item in self.candidates if item.disposition == "unchanged")


def _loads_object(value: str) -> dict[str, Any]:
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise PlanError("Stored plan object is invalid.")
    return loaded


def _loads_tags(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    loaded = json.loads(value)
    if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
        raise PlanError("Stored plan tags are invalid.")
    return tuple(loaded)


def _candidate_from_row(row: ChangePlanItem) -> PlanCandidate:
    after_tags = _loads_tags(row.after_tags_json)
    return PlanCandidate(
        work_id=row.work_id,
        subject_id=row.subject_id,
        title=row.title,
        bgm_url=row.bgm_url,
        disposition=row.disposition,
        reason=row.reason,
        action=_loads_object(row.action_json),
        selection_evidence=_loads_object(row.selection_evidence_json),
        before_snapshot=(
            CollectionSnapshot.from_mapping(json.loads(row.before_snapshot_json))
            if row.before_snapshot_json is not None
            else None
        ),
        intended_snapshot=(
            CollectionSnapshot.from_mapping(json.loads(row.intended_snapshot_json))
            if row.intended_snapshot_json is not None
            else None
        ),
        before_tags=_loads_tags(row.before_tags_json) or (),
        after_tags=after_tags,
        public_tags=_loads_tags(row.public_tags_json) or (),
        precondition_hash=row.precondition_hash,
        changed_fields=tuple(json.loads(row.changed_fields_json)),
        local_precondition_hash=row.local_precondition_hash,
        source_entry_id=row.source_entry_id,
        source_precondition_hash=row.source_precondition_hash,
        remote_existence=row.remote_existence,
    )


def load_plan(session: Session, plan_id: str, *, verify: bool = True) -> StoredPlan:
    plan = session.get(ChangePlan, plan_id)
    if plan is None:
        raise PlanError(f"Plan not found: {plan_id}")
    rows = session.scalars(
        select(ChangePlanItem)
        .where(ChangePlanItem.plan_id == plan_id)
        .order_by(ChangePlanItem.id)
    ).all()
    candidates = tuple(_candidate_from_row(row) for row in rows)
    stored = StoredPlan(plan=plan, candidates=candidates)
    if verify:
        verify_plan(stored)
    return stored


def verify_plan(stored: StoredPlan) -> None:
    plan = stored.plan
    content = plan_content(
        kind=plan.kind,
        operation=plan.operation,
        tag=plan.tag,
        old_tag=plan.old_tag,
        new_tag=plan.new_tag,
        selector=_loads_object(plan.selector_json),
        candidates=list(stored.candidates),
        reverse_of_plan_id=plan.reverse_of_plan_id,
        format_version=plan.format_version,
    )
    if content_digest(content) != plan.content_hash:
        raise PlanError(f"Plan {plan.id} failed immutable content hash verification.")


def _save_plan(
    session: Session,
    *,
    kind: str,
    operation: str,
    selector: dict[str, Any],
    candidates: list[PlanCandidate],
    tag: str | None = None,
    old_tag: str | None = None,
    new_tag: str | None = None,
    created_by: str = "manual",
    reverse_of_plan_id: str | None = None,
    format_version: int = 1,
) -> StoredPlan:
    plan_id = str(uuid4())
    content = plan_content(
        kind=kind,
        operation=operation,
        tag=tag,
        old_tag=old_tag,
        new_tag=new_tag,
        selector=selector,
        candidates=candidates,
        reverse_of_plan_id=reverse_of_plan_id,
        format_version=format_version,
    )
    planned_count = sum(item.disposition == "planned" for item in candidates)
    summary = {
        "evaluated": len(candidates),
        "planned": planned_count,
        "unchanged": len(candidates) - planned_count,
    }
    plan = ChangePlan(
        id=plan_id,
        format_version=format_version,
        kind=kind,
        operation=operation,
        tag=tag,
        old_tag=old_tag,
        new_tag=new_tag,
        selector_json=stable_json(selector),
        summary_json=stable_json(summary),
        content_hash=content_digest(content),
        status="draft",
        created_by=created_by,
        reverse_of_plan_id=reverse_of_plan_id,
        created_at=utc_now_iso(),
    )
    session.add(plan)
    session.flush()
    for candidate in candidates:
        before = candidate.before_snapshot
        if before is None and format_version < 3:
            before = CollectionSnapshot.create(
                collection_type=None, rating=None, comment=None, is_private=False, tags=()
            )
        session.add(
            ChangePlanItem(
                plan_id=plan_id,
                work_id=candidate.work_id,
                subject_id=candidate.subject_id,
                title=candidate.title,
                bgm_url=candidate.bgm_url,
                disposition=candidate.disposition,
                reason=candidate.reason,
                action_json=stable_json(candidate.action),
                selection_evidence_json=stable_json(candidate.selection_evidence),
                before_snapshot_json=before.to_json() if before is not None else None,
                intended_snapshot_json=(
                    candidate.intended_snapshot.to_json()
                    if candidate.intended_snapshot is not None
                    else None
                ),
                before_tags_json=stable_json(list(candidate.before_tags)),
                after_tags_json=(
                    stable_json(list(candidate.after_tags))
                    if candidate.after_tags is not None
                    else None
                ),
                public_tags_json=stable_json(list(candidate.public_tags)),
                precondition_hash=candidate.precondition_hash,
                changed_fields_json=stable_json(list(candidate.changed_fields)),
                local_precondition_hash=candidate.local_precondition_hash,
                source_entry_id=candidate.source_entry_id,
                source_precondition_hash=candidate.source_precondition_hash,
                remote_existence=candidate.remote_existence,
                item_status="pending" if candidate.disposition == "planned" else "not_applicable",
            )
        )
    session.flush()
    return load_plan(session, plan_id)


def save_plan(
    session: Session,
    *,
    kind: str,
    operation: str,
    selector: dict[str, Any],
    candidates: list[PlanCandidate],
    tag: str | None = None,
    old_tag: str | None = None,
    new_tag: str | None = None,
    created_by: str = "manual",
    reverse_of_plan_id: str | None = None,
    format_version: int = 1,
) -> StoredPlan:
    """Persist an immutable plan through the shared hash-compatible writer.

    New plan families use this public boundary instead of depending on the
    historical private helper.  The helper remains unchanged so v1-v4 hashes
    and fixtures retain their exact representation.
    """

    return _save_plan(
        session,
        kind=kind,
        operation=operation,
        selector=selector,
        candidates=candidates,
        tag=tag,
        old_tag=old_tag,
        new_tag=new_tag,
        created_by=created_by,
        reverse_of_plan_id=reverse_of_plan_id,
        format_version=format_version,
    )


def local_tag_safety_reason(session: Session, work: Work) -> str | None:
    identity = session.scalar(
        select(BangumiSubject).where(BangumiSubject.work_id == work.id)
    )
    if identity is None:
        return "missing_bangumi_subject"
    shadow = session.get(SyncShadow, identity.subject_id)
    if shadow is None:
        return "missing_shadow"
    open_tag_conflict = session.scalar(
        select(SyncConflict.id).where(
            SyncConflict.subject_id == identity.subject_id,
            SyncConflict.field == "tags",
            SyncConflict.status == "open",
        )
    )
    if open_tag_conflict is not None:
        return "local_tag_conflict"
    if local_snapshot(session, identity.subject_id).tags != shadow_snapshot(shadow).tags:
        return "local_tags_changed"
    return None


def _public_tag_match(tags: tuple[str, ...], target: str) -> bool:
    normalized = target.strip().casefold()
    return any(tag.strip().casefold() == normalized for tag in tags)


def _public_tags(
    remote: RemoteCollection,
    target: str,
    detail_loader: Callable[[int], tuple[str, ...]],
) -> tuple[tuple[str, ...], str]:
    slim = remote.subject.public_tags
    if _public_tag_match(slim, target):
        return slim, "collection_slim"
    detailed = detail_loader(remote.subject_id)
    return detailed, "subject_detail"


def _works_by_subject(
    session: Session,
    subject_ids: tuple[int, ...],
    *,
    kind: str | None = None,
) -> dict[int, tuple[Work, BangumiSubject]]:
    statement = (
        select(Work, BangumiSubject)
        .join(BangumiSubject, BangumiSubject.work_id == Work.id)
        .where(BangumiSubject.subject_id.in_(subject_ids))
    )
    if kind is not None:
        statement = statement.where(Work.kind == kind)
    return {identity.subject_id: (work, identity) for work, identity in session.execute(statement)}


def create_bulk_tag_plan(
    session: Session,
    collections: list[RemoteCollection],
    *,
    operation: str,
    selector: dict[str, Any],
    detail_loader: Callable[[int], tuple[str, ...]],
    tag: str | None = None,
    old_tag: str | None = None,
    new_tag: str | None = None,
) -> StoredPlan:
    if operation in ("add", "remove"):
        if tag is None:
            raise PlanError("A tag is required.")
        tag = validate_tag(tag)
    elif operation == "rename":
        if old_tag is None or new_tag is None:
            raise PlanError("Both old and new tags are required.")
        old_tag = validate_tag(old_tag)
        new_tag = validate_tag(new_tag)
        if old_tag == new_tag:
            raise PlanError("Old and new tags must be different.")
    else:
        raise PlanError(f"Unsupported bulk operation: {operation}")

    mode = str(selector.get("mode"))
    raw_subject_type = selector.get("subject_type")
    try:
        subject_type = (
            SubjectType.parse(str(raw_subject_type))
            if raw_subject_type not in (None, "")
            else None
        )
    except ValueError as exc:
        raise PlanError(str(exc)) from None
    scoped_collections = [
        item
        for item in collections
        if subject_type is None or int(item.subject_type) == int(subject_type)
    ]
    remote_by_id = {item.subject_id: item for item in scoped_collections}
    if mode == "ids":
        requested = tuple(int(value) for value in selector.get("ids", []))
        missing = sorted(set(requested) - remote_by_id.keys())
        if missing:
            scope = f" within subject type {subject_type.kind}" if subject_type else ""
            raise PlanError(
                f"Subject IDs are not current remote collections{scope}: {missing}"
            )
        universe = [remote_by_id[value] for value in requested]
    elif mode in ("all_current", "public_tag"):
        universe = sorted(scoped_collections, key=lambda item: item.subject_id)
    else:
        raise PlanError("Choose exactly one selector: ids, all_current, or public_tag.")

    works = _works_by_subject(session, tuple(remote_by_id))
    missing_local = sorted(item.subject_id for item in universe if item.subject_id not in works)
    if missing_local:
        raise PlanError(
            f"Remote collections are not mirrored locally: {missing_local}. Run 'bld pull' first."
        )

    action = {"operation": operation, "tag": tag, "old_tag": old_tag, "new_tag": new_tag}
    candidates: list[PlanCandidate] = []
    for remote in universe:
        work, identity = works[remote.subject_id]
        before = snapshot_from_remote(remote)
        before_tags = tuple(remote.tags)
        public_tags = remote.subject.public_tags
        evidence: dict[str, Any] = {"selector": mode}
        if subject_type is not None:
            evidence["subject_type"] = int(subject_type)
        reason = local_tag_safety_reason(session, work)

        if reason is None and mode == "public_tag":
            public_target = validate_tag(str(selector["public_tag"]))
            try:
                public_tags, source = _public_tags(remote, public_target, detail_loader)
                evidence.update({"public_tag": public_target, "public_tag_source": source})
                if not _public_tag_match(public_tags, public_target):
                    reason = "public_tag_not_matched"
            except BangumiAPIError as exc:
                if exc.status_code in (401, 403):
                    raise
                reason = "public_tag_read_failed"

        after_tags = apply_tag_action(
            before_tags,
            operation,
            tag=tag,
            old_tag=old_tag,
            new_tag=new_tag,
        )
        intended = before.replacing({"tags": after_tags})
        disposition = "planned" if reason is None and after_tags != before_tags else "unchanged"
        if reason is None and disposition == "unchanged":
            reason = "no_op"
        candidates.append(
            PlanCandidate(
                work_id=work.id,
                subject_id=remote.subject_id,
                title=work.title,
                bgm_url=identity.url,
                disposition=disposition,
                reason=reason or "will_modify",
                action=action,
                selection_evidence=evidence,
                before_snapshot=before,
                intended_snapshot=intended,
                before_tags=before_tags,
                after_tags=after_tags,
                public_tags=public_tags,
                precondition_hash=before.digest() if disposition == "planned" else None,
            )
        )
    return _save_plan(
        session,
        kind=f"bulk_{operation}",
        operation=operation,
        selector=selector,
        candidates=candidates,
        tag=tag,
        old_tag=old_tag,
        new_tag=new_tag,
    )


def create_classification_plan(
    session: Session,
    collections: list[RemoteCollection],
    *,
    public_tag: str,
    galgame_tag: str,
    game_tag: str,
    detail_loader: Callable[[int], tuple[str, ...]],
) -> StoredPlan:
    public_tag = validate_tag(public_tag)
    galgame_tag = validate_tag(galgame_tag)
    game_tag = validate_tag(game_tag)
    if galgame_tag == game_tag:
        raise PlanError("Galgame and Game classification tags must differ.")
    selector = {
        "mode": "classify_games",
        "public_tag": public_tag,
        "galgame_tag": galgame_tag,
        "game_tag": game_tag,
    }
    game_collections = [item for item in collections if int(item.subject_type) == 4]
    games = _works_by_subject(
        session, tuple(item.subject_id for item in game_collections), kind="game"
    )
    missing_local = sorted(
        item.subject_id for item in game_collections if item.subject_id not in games
    )
    if missing_local:
        raise PlanError(
            f"Remote collections are not mirrored locally: {missing_local}. Run 'bgv pull' first."
        )

    action = {"operation": "add", "tag": galgame_tag, "old_tag": None, "new_tag": None}
    candidates: list[PlanCandidate] = []
    for remote in sorted(game_collections, key=lambda item: item.subject_id):
        work, identity = games[remote.subject_id]
        before = snapshot_from_remote(remote)
        before_tags = tuple(remote.tags)
        public_tags = remote.subject.public_tags
        evidence: dict[str, Any] = {
            "selector": "classify_games",
            "public_tag": public_tag,
        }
        reason = local_tag_safety_reason(session, work)
        if reason is None and galgame_tag in before_tags:
            reason = "already_galgame"
        if reason is None and game_tag in before_tags:
            reason = "already_game"
        if reason is None:
            try:
                public_tags, source = _public_tags(remote, public_tag, detail_loader)
                evidence["public_tag_source"] = source
                if not _public_tag_match(public_tags, public_tag):
                    reason = "public_tag_not_matched_manual_review"
            except BangumiAPIError as exc:
                if exc.status_code in (401, 403):
                    raise
                reason = "public_tag_read_failed"

        after_tags = apply_tag_action(before_tags, "add", tag=galgame_tag)
        intended = before.replacing({"tags": after_tags})
        disposition = "planned" if reason is None else "unchanged"
        candidates.append(
            PlanCandidate(
                work_id=work.id,
                subject_id=remote.subject_id,
                title=work.title,
                bgm_url=identity.url,
                disposition=disposition,
                reason=reason or "public_tag_matched",
                action=action,
                selection_evidence=evidence,
                before_snapshot=before,
                intended_snapshot=intended,
                before_tags=before_tags,
                after_tags=after_tags,
                public_tags=public_tags,
                precondition_hash=before.digest() if disposition == "planned" else None,
            )
        )
    return _save_plan(
        session,
        kind="classify_games",
        operation="add",
        selector=selector,
        candidates=candidates,
        tag=galgame_tag,
    )


def create_sync_plan(
    session: Session,
    collections: list[RemoteCollection],
    *,
    selector: dict[str, Any],
    fields: tuple[str, ...] = CANONICAL_FIELDS,
) -> StoredPlan:
    selected_fields = tuple(dict.fromkeys(fields))
    if not selected_fields:
        raise PlanError("At least one sync field is required.")
    unknown = set(selected_fields) - set(CANONICAL_FIELDS)
    if unknown:
        raise PlanError(f"Unknown sync fields: {sorted(unknown)}")

    remote_by_id = {item.subject_id: item for item in collections}
    mode = str(selector.get("mode"))
    statement = select(Work, BangumiSubject).join(
        BangumiSubject, BangumiSubject.work_id == Work.id
    )
    if mode == "ids":
        requested = tuple(dict.fromkeys(int(value) for value in selector.get("ids", [])))
        if not requested:
            raise PlanError("At least one subject ID is required.")
        statement = statement.where(BangumiSubject.subject_id.in_(requested))
    elif mode != "all_local_changes":
        raise PlanError("Choose ids or all_local_changes for a sync plan.")
    rows = sorted(session.execute(statement).all(), key=lambda row: row[1].subject_id)
    if mode == "ids":
        found = {identity.subject_id for _, identity in rows}
        missing = sorted(set(requested) - found)
        if missing:
            raise PlanError(f"Subject IDs are not mirrored locally: {missing}")

    candidates: list[PlanCandidate] = []
    for work, identity in rows:
        remote_record = remote_by_id.get(identity.subject_id)
        shadow = session.get(SyncShadow, identity.subject_id)
        local = local_snapshot(session, identity.subject_id)
        base = shadow_snapshot(shadow) if shadow is not None else None
        evidence: dict[str, Any] = {
            "selector": mode,
            "evaluated_fields": list(selected_fields),
            "field_statuses": {},
        }
        if remote_record is None:
            candidates.append(
                PlanCandidate(
                    work_id=work.id,
                    subject_id=identity.subject_id,
                    title=work.title,
                    bgm_url=identity.url,
                    disposition="unchanged",
                    reason="remote_missing",
                    action={"operation": "patch", "fields": [], "values": {}},
                    selection_evidence=evidence,
                    before_snapshot=base or local,
                    intended_snapshot=None,
                    before_tags=(base or local).tags,
                    after_tags=None,
                    precondition_hash=None,
                    changed_fields=(),
                    local_precondition_hash=local.digest(),
                )
            )
            continue
        remote = snapshot_from_remote(remote_record)
        if base is None:
            reason = "missing_shadow"
            safe_fields: tuple[str, ...] = ()
        else:
            from bangumi_local.domain.merge import DiffStatus, diff_collection

            diff = diff_collection(base, local, remote)
            field_statuses = {item.field: item.status.value for item in diff.fields}
            evidence["field_statuses"] = field_statuses
            safe_fields = tuple(
                field
                for field in selected_fields
                if field_statuses[field] == DiffStatus.LOCAL_CHANGED.value
            )
            if safe_fields:
                reason = "local_changed_remote_unchanged"
            elif any(field_statuses[field] == DiffStatus.CONFLICT.value for field in selected_fields):
                reason = "conflict"
            elif any(
                field_statuses[field] == DiffStatus.REMOTE_CHANGED.value
                for field in selected_fields
            ):
                reason = "remote_changed_pull_required"
            elif any(field_statuses[field] == DiffStatus.CONVERGED.value for field in selected_fields):
                reason = "converged_pull_required"
            else:
                reason = "no_op"

        values = {field: local.value_for(field) for field in safe_fields}
        intended = remote.replacing(values) if safe_fields else None
        disposition = "planned" if safe_fields else "unchanged"
        candidates.append(
            PlanCandidate(
                work_id=work.id,
                subject_id=identity.subject_id,
                title=work.title,
                bgm_url=identity.url,
                disposition=disposition,
                reason=reason,
                action={"operation": "patch", "fields": list(safe_fields), "values": values},
                selection_evidence=evidence,
                before_snapshot=remote,
                intended_snapshot=intended,
                before_tags=tuple(remote_record.tags),
                after_tags=intended.tags if intended is not None else None,
                public_tags=remote_record.subject.public_tags,
                precondition_hash=remote.digest() if safe_fields else None,
                changed_fields=safe_fields,
                local_precondition_hash=local.digest(),
            )
        )
    return _save_plan(
        session,
        kind="sync",
        operation="patch",
        selector={**selector, "fields": list(selected_fields)},
        candidates=candidates,
        format_version=2,
    )


def review_plan(session: Session, plan_id: str) -> StoredPlan:
    stored = load_plan(session, plan_id)
    if stored.plan.status != "draft":
        raise PlanError(f"Plan must be draft to review; current status is {stored.plan.status}.")
    if not stored.planned:
        raise PlanError("Plan has no actionable items and cannot be reviewed.")
    stored.plan.status = "reviewed"
    stored.plan.reviewed_at = utc_now_iso()
    session.flush()
    return stored


def plan_as_dict(stored: StoredPlan) -> dict[str, object]:
    plan = stored.plan
    return {
        "id": plan.id,
        "kind": plan.kind,
        "operation": plan.operation,
        "tag": plan.tag,
        "old_tag": plan.old_tag,
        "new_tag": plan.new_tag,
        "selector": json.loads(plan.selector_json),
        "summary": json.loads(plan.summary_json),
        "content_hash": plan.content_hash,
        "status": plan.status,
        "created_by": plan.created_by,
        "reverse_of_plan_id": plan.reverse_of_plan_id,
        "created_at": plan.created_at,
        "reviewed_at": plan.reviewed_at,
        "applied_at": plan.applied_at,
        "format_version": plan.format_version,
        "items": [
            candidate.immutable_dict(format_version=plan.format_version)
            for candidate in stored.candidates
        ],
    }


def export_plan(stored: StoredPlan, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{stored.plan.id}.json"
    csv_path = directory / f"{stored.plan.id}.csv"
    json_path.write_text(
        json.dumps(plan_as_dict(stored), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "work_id",
                "subject_id",
                "source_entry_id",
                "title",
                "bgm_url",
                "disposition",
                "reason",
                "selection_evidence",
                "changed_fields",
                "action",
                "before_snapshot",
                "intended_snapshot",
                "before_tags",
                "after_tags",
                "public_tags",
                "precondition_hash",
                "local_precondition_hash",
                "source_precondition_hash",
                "remote_existence",
            ),
        )
        writer.writeheader()
        for item in stored.candidates:
            writer.writerow(
                {
                    "work_id": item.work_id,
                    "subject_id": item.subject_id,
                    "source_entry_id": item.source_entry_id,
                    "title": item.title,
                    "bgm_url": item.bgm_url,
                    "disposition": item.disposition,
                    "reason": item.reason,
                    "selection_evidence": stable_json(item.selection_evidence),
                    "changed_fields": stable_json(list(item.changed_fields)),
                    "action": stable_json(item.action),
                    "before_snapshot": stable_json(
                        item.before_snapshot.as_dict()
                        if item.before_snapshot is not None
                        else {}
                    ),
                    "intended_snapshot": stable_json(
                        item.intended_snapshot.as_dict()
                        if item.intended_snapshot is not None
                        else None
                    ),
                    "before_tags": stable_json(list(item.before_tags)),
                    "after_tags": stable_json(list(item.after_tags or ())),
                    "public_tags": stable_json(list(item.public_tags)),
                    "precondition_hash": item.precondition_hash or "",
                    "local_precondition_hash": item.local_precondition_hash or "",
                    "source_precondition_hash": item.source_precondition_hash or "",
                    "remote_existence": item.remote_existence or "",
                }
            )
    return json_path, csv_path


def persist_reverse_plan(
    session: Session,
    *,
    source_plan: ChangePlan,
    candidates: list[PlanCandidate],
) -> StoredPlan | None:
    if not candidates:
        return None
    return _save_plan(
        session,
        kind="reverse",
        operation="set" if source_plan.format_version == 1 else "patch",
        selector={"mode": "successful_items", "source_plan_id": source_plan.id},
        candidates=candidates,
        created_by="system",
        reverse_of_plan_id=source_plan.id,
        format_version=source_plan.format_version,
    )


def create_recovery_plan(
    session: Session,
    source_plan_id: str,
    collections: list[RemoteCollection],
) -> StoredPlan:
    """Create a set-tags draft for audited writes that reached an unexpected state."""
    source = load_plan(session, source_plan_id)
    source_by_id = {item.subject_id: item for item in source.planned}
    operations = session.scalars(
        select(RemoteOperation)
        .where(
            RemoteOperation.plan_id == source_plan_id,
            RemoteOperation.status == "uncertain",
            RemoteOperation.actual_snapshot_json.is_not(None),
        )
        .order_by(RemoteOperation.started_at.desc())
    ).all()
    latest_by_id: dict[int, RemoteOperation] = {}
    for operation in operations:
        latest_by_id.setdefault(operation.subject_id, operation)
    if not latest_by_id:
        raise PlanError("The source plan has no audited uncertain remote state to recover.")

    remote_by_id = {item.subject_id: item for item in collections}
    missing = sorted(set(latest_by_id) - remote_by_id.keys())
    if missing:
        raise PlanError(f"Recovery targets are no longer current remote collections: {missing}")

    candidates: list[PlanCandidate] = []
    for subject_id, operation in sorted(latest_by_id.items()):
        source_item = source_by_id.get(subject_id)
        if source_item is None:
            raise PlanError(f"Audited operation has no immutable source item: {subject_id}")
        remote = remote_by_id[subject_id]
        current = snapshot_from_remote(remote)
        original = source_item.before_snapshot
        if original is None:
            raise PlanError(f"Source item has no before snapshot: {subject_id}")
        audited_actual = CollectionSnapshot.from_mapping(
            json.loads(operation.actual_snapshot_json or "{}")
        )
        if current == original:
            disposition = "unchanged"
            reason = "already_restored"
        elif current != audited_actual:
            disposition = "unchanged"
            reason = "recovery_stale"
        else:
            disposition = "planned"
            reason = "recover_unexpected_remote_state"
        after_tags = source_item.before_tags
        candidates.append(
            PlanCandidate(
                work_id=source_item.work_id,
                subject_id=subject_id,
                title=source_item.title,
                bgm_url=source_item.bgm_url,
                disposition=disposition,
                reason=reason,
                action={"operation": "set", "set_tags": list(after_tags)},
                selection_evidence={
                    "source_plan_id": source_plan_id,
                    "source_operation_id": operation.id,
                },
                before_snapshot=current,
                intended_snapshot=current.replacing({"tags": after_tags}),
                before_tags=tuple(remote.tags),
                after_tags=after_tags,
                public_tags=remote.subject.public_tags,
                precondition_hash=current.digest() if disposition == "planned" else None,
            )
        )
    return _save_plan(
        session,
        kind="recovery",
        operation="set",
        selector={"mode": "uncertain_operations", "source_plan_id": source_plan_id},
        candidates=candidates,
        created_by="system",
        reverse_of_plan_id=source_plan_id,
    )

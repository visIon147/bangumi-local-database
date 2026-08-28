from __future__ import annotations

import json
from dataclasses import replace

from sqlalchemy.orm import Session

from bangumi_local.domain.plans import PlanCandidate
from bangumi_local.domain.tags import apply_tag_action
from bangumi_local.services.plans import PlanError, StoredPlan, load_plan, save_plan


_REVISION_KINDS = {
    "bulk_add",
    "bulk_remove",
    "bulk_rename",
    "classify_games",
    "sync",
    "steam_status",
    "discovery_status",
    "pull",
}


def _original_payload(candidate: PlanCandidate) -> dict[str, object]:
    return {
        "disposition": candidate.disposition,
        "reason": candidate.reason,
        "action": candidate.action,
        "intended_snapshot": (
            candidate.intended_snapshot.as_dict()
            if candidate.intended_snapshot is not None
            else None
        ),
        "after_tags": list(candidate.after_tags) if candidate.after_tags is not None else None,
        "precondition_hash": candidate.precondition_hash,
        "changed_fields": list(candidate.changed_fields),
    }


def _with_original(candidate: PlanCandidate) -> tuple[PlanCandidate, dict[str, object]]:
    evidence = dict(candidate.selection_evidence)
    payload = evidence.get("review_original")
    if not isinstance(payload, dict):
        payload = _original_payload(candidate)
        evidence["review_original"] = payload
    return candidate, evidence


def _restore(candidate: PlanCandidate, evidence: dict[str, object]) -> PlanCandidate:
    payload = evidence.get("review_original")
    if not isinstance(payload, dict):
        return replace(candidate, selection_evidence=evidence)
    intended = payload.get("intended_snapshot")
    after_tags = payload.get("after_tags")
    from bangumi_local.domain.snapshots import CollectionSnapshot

    return replace(
        candidate,
        disposition=str(payload.get("disposition", candidate.disposition)),
        reason=str(payload.get("reason", candidate.reason)),
        action=dict(payload.get("action") or candidate.action),
        selection_evidence=evidence,
        intended_snapshot=(
            CollectionSnapshot.from_mapping(intended) if isinstance(intended, dict) else None
        ),
        after_tags=(
            tuple(str(value) for value in after_tags)
            if isinstance(after_tags, list)
            else None
        ),
        precondition_hash=(
            str(payload["precondition_hash"])
            if payload.get("precondition_hash") is not None
            else None
        ),
        changed_fields=tuple(str(value) for value in payload.get("changed_fields", [])),
    )


def _excluded(candidate: PlanCandidate, evidence: dict[str, object]) -> PlanCandidate:
    return replace(
        candidate,
        disposition="unchanged",
        reason="user_excluded",
        selection_evidence=evidence,
        precondition_hash=None,
        changed_fields=(),
    )


def revise_plan_selection(
    session: Session,
    plan_id: str,
    *,
    included_subject_ids: set[int],
    selected_fields: dict[int, tuple[str, ...]] | None = None,
    classification_decisions: dict[int, str] | None = None,
) -> StoredPlan:
    stored = load_plan(session, plan_id)
    plan = stored.plan
    if plan.status != "draft":
        raise PlanError("Only a draft plan can create a revised successor.")
    if plan.kind not in _REVISION_KINDS:
        raise PlanError("This plan type is read-only in the visual workbench.")
    selected_fields = selected_fields or {}
    classification_decisions = classification_decisions or {}
    selector = json.loads(plan.selector_json)
    if not isinstance(selector, dict):
        raise PlanError("Stored plan selector is invalid.")
    root = selector.get("revision_root_plan_id") or selector.get("revision_of_plan_id") or plan_id
    revised: list[PlanCandidate] = []

    for candidate in stored.candidates:
        _, evidence = _with_original(candidate)
        original = _restore(candidate, evidence)
        subject_id = candidate.subject_id
        if subject_id is None:
            revised.append(candidate)
            continue

        if plan.kind == "classify_games" and subject_id in classification_decisions:
            decision = classification_decisions[subject_id]
            if decision in {"galgame", "game"}:
                if candidate.before_snapshot is None:
                    raise PlanError(f"Classification item {subject_id} has no before snapshot.")
                tag_key = "galgame_tag" if decision == "galgame" else "game_tag"
                tag = str(selector.get(tag_key, ""))
                if not tag:
                    raise PlanError("Classification selector is missing its personal Tag.")
                after = apply_tag_action(candidate.before_tags, "add", tag=tag)
                revised.append(
                    replace(
                        candidate,
                        disposition="planned",
                        reason="manual_classification",
                        action={"operation": "add", "tag": tag, "old_tag": None, "new_tag": None},
                        selection_evidence={**evidence, "manual_classification": decision},
                        intended_snapshot=candidate.before_snapshot.replacing({"tags": after}),
                        after_tags=after,
                        precondition_hash=candidate.before_snapshot.digest(),
                        changed_fields=(),
                    )
                )
                continue
            if decision in {"defer", "exclude"}:
                revised.append(_excluded(candidate, {**evidence, "manual_classification": decision}))
                continue

        if original.disposition != "planned":
            revised.append(replace(candidate, selection_evidence=evidence))
            continue
        if subject_id not in included_subject_ids:
            revised.append(_excluded(original, evidence))
            continue

        restored = original
        if plan.kind == "sync" and subject_id in selected_fields:
            requested = tuple(dict.fromkeys(selected_fields[subject_id]))
            allowed = set(original.changed_fields)
            if not set(requested).issubset(allowed):
                raise PlanError(f"Sync fields for subject {subject_id} exceed the original safe set.")
            if not requested:
                revised.append(_excluded(original, evidence))
                continue
            values = dict(original.action.get("values", {}))
            values = {field: values[field] for field in requested}
            if original.before_snapshot is None:
                raise PlanError(f"Sync item {subject_id} has no remote before snapshot.")
            restored = replace(
                original,
                action={"operation": "patch", "fields": list(requested), "values": values},
                intended_snapshot=original.before_snapshot.replacing(values),
                after_tags=(
                    tuple(values["tags"])
                    if "tags" in values
                    else original.before_snapshot.tags
                ),
                changed_fields=requested,
            )
        revised.append(replace(restored, selection_evidence=evidence))

    successor = save_plan(
        session,
        kind=plan.kind,
        operation=plan.operation,
        selector={
            **selector,
            "revision_of_plan_id": plan_id,
            "revision_root_plan_id": root,
        },
        candidates=revised,
        tag=plan.tag,
        old_tag=plan.old_tag,
        new_tag=plan.new_tag,
        created_by="manual",
        format_version=plan.format_version,
    )
    plan.status = "cancelled"
    session.flush()
    return successor

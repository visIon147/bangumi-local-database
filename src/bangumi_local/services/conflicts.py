from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from bangumi_local.db.models import SyncConflict, SyncShadow
from bangumi_local.db.repositories import (
    apply_remote_fields,
    local_snapshot,
    shadow_snapshot,
    utc_now_iso,
    write_shadow,
)
from bangumi_local.domain.snapshots import CollectionSnapshot


class ConflictResolutionError(ValueError):
    """Raised when a conflict cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class ConflictResolutionResult:
    conflict_id: int
    subject_id: int
    field: str
    strategy: str
    local_before: CollectionSnapshot
    local_after: CollectionSnapshot
    base_after: CollectionSnapshot


def resolve_conflict(
    session: Session,
    conflict_id: int,
    *,
    strategy: str,
    custom_value: object | None = None,
) -> ConflictResolutionResult:
    """Resolve one recorded field conflict without contacting Bangumi.

    The conflict's observed remote value becomes the new BASE.  Keeping LOCAL
    (or choosing a custom value) therefore becomes an explicit outgoing local
    change on the next fresh sync plan, while keeping REMOTE converges LOCAL.
    """

    conflict = session.get(SyncConflict, conflict_id)
    if conflict is None:
        raise ConflictResolutionError(f"Conflict does not exist: {conflict_id}")
    if conflict.status != "open":
        raise ConflictResolutionError("Only open conflicts can be resolved.")
    if strategy not in {"keep_local", "keep_remote", "custom"}:
        raise ConflictResolutionError(
            "strategy must be keep_local, keep_remote, or custom"
        )
    if strategy != "custom" and custom_value is not None:
        raise ConflictResolutionError("custom_value is only valid with custom strategy.")

    shadow = session.get(SyncShadow, conflict.subject_id)
    if shadow is None:
        raise ConflictResolutionError("Conflict has no synchronization shadow.")
    local_before = local_snapshot(session, conflict.subject_id)
    base_before = shadow_snapshot(shadow)
    remote_value = json.loads(conflict.remote_json)

    # Validate the recorded field/value through the canonical snapshot model.
    base_after = base_before.replacing({conflict.field: remote_value})
    if strategy == "keep_remote":
        local_after = local_before.replacing({conflict.field: remote_value})
        apply_remote_fields(
            session, conflict.subject_id, local_after, (conflict.field,)
        )
    elif strategy == "custom":
        local_after = local_before.replacing({conflict.field: custom_value})
        apply_remote_fields(
            session, conflict.subject_id, local_after, (conflict.field,)
        )
    else:
        local_after = local_before

    write_shadow(
        session,
        conflict.subject_id,
        base_after,
        shadow.remote_updated_at or utc_now_iso(),
    )
    conflict.status = "resolved"
    conflict.resolution = strategy
    conflict.resolved_at = utc_now_iso()
    return ConflictResolutionResult(
        conflict_id=conflict.id,
        subject_id=conflict.subject_id,
        field=conflict.field,
        strategy=strategy,
        local_before=local_before,
        local_after=local_after,
        base_after=base_after,
    )

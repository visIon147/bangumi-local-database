from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from bangumi_local.db.models import BangumiSubject, SyncShadow
from bangumi_local.db.repositories import (
    apply_remote_fields,
    apply_remote_snapshot,
    ensure_bangumi_link,
    ensure_state,
    local_snapshot,
    persist_conflicts,
    shadow_snapshot,
    upsert_work,
    write_shadow,
)
from bangumi_local.domain.merge import DiffStatus, advance_base_after_pull, diff_collection
from bangumi_local.domain.models import RemoteCollection, SubjectType
from bangumi_local.domain.snapshots import CollectionSnapshot


@dataclass(slots=True)
class PullResult:
    remote_count: int = 0
    imported: int = 0
    remote_updates: int = 0
    metadata_updates: int = 0
    unchanged: int = 0
    local_changes_preserved: int = 0
    converged: int = 0
    conflicts: int = 0
    missing_remote: int = 0
    bootstrapped: int = 0
    bootstrap_mismatches: int = 0
    conflict_records_created: int = 0
    by_subject_type: dict[int, int] = field(default_factory=dict)


def snapshot_from_remote(remote: RemoteCollection) -> CollectionSnapshot:
    return CollectionSnapshot.create(
        collection_type=int(remote.status),
        rating=remote.rate,
        comment=remote.comment,
        is_private=remote.private,
        tags=remote.tags,
    )


def pull_collections(
    session: Session,
    collections: list[RemoteCollection],
    *,
    scope_subject_type: SubjectType | None = None,
) -> PullResult:
    deduplicated = {item.subject_id: item for item in collections}
    result = PullResult(remote_count=len(deduplicated))
    seen_subject_ids: set[int] = set()

    for remote in deduplicated.values():
        work, metadata_changed, work_created = upsert_work(session, remote)
        result.by_subject_type[int(remote.subject_type)] = (
            result.by_subject_type.get(int(remote.subject_type), 0) + 1
        )
        if metadata_changed:
            result.metadata_updates += 1
        ensure_state(session, remote.subject_id)
        ensure_bangumi_link(session, work, remote.subject_id)
        if remote.rate:
            from bangumi_local.services.rating_queue import observe_explicit_rating

            observe_explicit_rating(
                session, remote.subject_id, remote.rate, source="remote_observed"
            )
        seen_subject_ids.add(remote.subject_id)

        remote_snapshot = snapshot_from_remote(remote)
        shadow = session.get(SyncShadow, remote.subject_id)
        if shadow is None:
            local = local_snapshot(session, remote.subject_id)
            if work_created:
                apply_remote_snapshot(session, remote.subject_id, remote_snapshot)
                write_shadow(session, remote.subject_id, remote_snapshot, remote.updated_at_utc)
                result.imported += 1
            elif local == remote_snapshot:
                write_shadow(session, remote.subject_id, remote_snapshot, remote.updated_at_utc)
                result.bootstrapped += 1
            else:
                result.bootstrap_mismatches += 1
            continue

        base = shadow_snapshot(shadow)
        local = local_snapshot(session, remote.subject_id)
        diff = diff_collection(base, local, remote_snapshot)
        result.conflict_records_created += persist_conflicts(session, remote.subject_id, diff)

        remote_fields = diff.fields_with_status(DiffStatus.REMOTE_CHANGED)
        if remote_fields:
            apply_remote_fields(session, remote.subject_id, remote_snapshot, remote_fields)
            result.remote_updates += 1

        next_base = advance_base_after_pull(diff)
        if next_base != base:
            write_shadow(session, remote.subject_id, next_base, remote.updated_at_utc)

        if diff.status == DiffStatus.CLEAN:
            result.unchanged += 1
        elif diff.status == DiffStatus.CONVERGED:
            result.converged += 1
        elif diff.status == DiffStatus.LOCAL_CHANGED:
            result.local_changes_preserved += 1
        elif diff.status == DiffStatus.CONFLICT:
            result.conflicts += 1

    shadow_statement = select(SyncShadow.subject_id).join(
        BangumiSubject, BangumiSubject.subject_id == SyncShadow.subject_id
    )
    if scope_subject_type is not None:
        shadow_statement = shadow_statement.where(
            BangumiSubject.subject_type == int(scope_subject_type)
        )
    all_shadow_subject_ids = set(session.scalars(shadow_statement).all())
    result.missing_remote = len(all_shadow_subject_ids - seen_subject_ids)
    return result

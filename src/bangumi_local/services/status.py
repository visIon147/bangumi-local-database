from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    SyncConflict,
    SyncShadow,
    Work,
)
from bangumi_local.db.repositories import local_snapshot, shadow_snapshot
from bangumi_local.domain.merge import CollectionDiff, DiffStatus, diff_collection
from bangumi_local.domain.models import RemoteCollection, SubjectType
from bangumi_local.domain.snapshots import CollectionSnapshot
from bangumi_local.services.pull import snapshot_from_remote


@dataclass(frozen=True, slots=True)
class StatusItem:
    work_id: int | None
    subject_id: int
    title: str
    bgm_url: str
    diff: CollectionDiff

    def to_dict(self) -> dict[str, object]:
        return {
            "work_id": self.work_id,
            "subject_id": self.subject_id,
            "title": self.title,
            "bgm_url": self.bgm_url,
            **self.diff.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class StatusReport:
    remote_source: str
    items: tuple[StatusItem, ...]

    @property
    def counts(self) -> dict[str, int]:
        result = {status.value: 0 for status in DiffStatus}
        for item in self.items:
            result[item.diff.status.value] += 1
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "remote_source": self.remote_source,
            "counts": self.counts,
            "items": [item.to_dict() for item in self.items],
        }


def _cached_remote_with_open_conflicts(
    base: CollectionSnapshot,
    conflicts: list[SyncConflict],
) -> CollectionSnapshot:
    values = {
        conflict.field: json.loads(conflict.remote_json)
        for conflict in conflicts
        if conflict.status == "open"
    }
    return base.replacing(values) if values else base


def build_status_report(
    session: Session,
    remote_collections: list[RemoteCollection] | None = None,
    *,
    subject_id: int | None = None,
    subject_type: SubjectType | None = None,
) -> StatusReport:
    remote_map = (
        {item.subject_id: item for item in remote_collections}
        if remote_collections is not None
        else None
    )
    statement = (
        select(Work, BangumiSubject)
        .join(BangumiSubject, BangumiSubject.work_id == Work.id)
        .join(
            BangumiCollectionState,
            BangumiCollectionState.subject_id == BangumiSubject.subject_id,
        )
        .order_by(BangumiSubject.subject_id)
    )
    if subject_id is not None:
        statement = statement.where(BangumiSubject.subject_id == subject_id)
    if subject_type is not None:
        statement = statement.where(BangumiSubject.subject_type == int(subject_type))
    rows = session.execute(statement).all()
    items: list[StatusItem] = []
    local_subject_ids: set[int] = set()

    for work, identity in rows:
        local_subject_ids.add(identity.subject_id)
        local = local_snapshot(session, identity.subject_id)
        shadow = session.get(SyncShadow, identity.subject_id)
        base = shadow_snapshot(shadow) if shadow else None
        if remote_map is not None:
            remote_record = remote_map.get(identity.subject_id)
            remote = snapshot_from_remote(remote_record) if remote_record else None
        elif base is not None:
            open_conflicts = session.scalars(
                select(SyncConflict).where(
                    SyncConflict.subject_id == identity.subject_id,
                    SyncConflict.status == "open",
                )
            ).all()
            remote = _cached_remote_with_open_conflicts(base, list(open_conflicts))
        else:
            remote = None
        items.append(
            StatusItem(
                work_id=work.id,
                subject_id=identity.subject_id,
                title=work.title,
                bgm_url=identity.url,
                diff=diff_collection(base, local, remote),
            )
        )

    if remote_map is not None:
        for remote_record in remote_map.values():
            if remote_record.subject_id in local_subject_ids:
                continue
            if subject_id is not None and remote_record.subject_id != subject_id:
                continue
            remote = snapshot_from_remote(remote_record)
            items.append(
                StatusItem(
                    work_id=None,
                    subject_id=remote_record.subject_id,
                    title=remote_record.subject.display_title,
                    bgm_url=f"https://bgm.tv/subject/{remote_record.subject_id}",
                    diff=diff_collection(None, remote, remote),
                )
            )

    return StatusReport(
        remote_source="fresh-remote" if remote_map is not None else "shadow-cache",
        items=tuple(items),
    )

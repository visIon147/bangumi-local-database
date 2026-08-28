from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from bangumi_local.db.models import BangumiCollectionState, BangumiSubject, SyncShadow, Work
from bangumi_local.db.repositories import local_snapshot, write_shadow
from bangumi_local.domain.models import RemoteCollection
from bangumi_local.domain.snapshots import CANONICAL_FIELDS
from bangumi_local.services.pull import snapshot_from_remote


@dataclass(frozen=True, slots=True)
class BootstrapItem:
    subject_id: int
    title: str
    outcome: str
    changed_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "subject_id": self.subject_id,
            "title": self.title,
            "outcome": self.outcome,
            "changed_fields": list(self.changed_fields),
        }


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    apply: bool
    items: tuple[BootstrapItem, ...]
    remote_only: int

    @property
    def counts(self) -> dict[str, int]:
        result = {
            "already_shadowed": 0,
            "eligible": 0,
            "applied": 0,
            "bootstrap_mismatch": 0,
            "remote_missing": 0,
            "remote_only": self.remote_only,
        }
        for item in self.items:
            result[item.outcome] += 1
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": "apply" if self.apply else "dry-run",
            "counts": self.counts,
            "items": [item.to_dict() for item in self.items],
        }


def bootstrap_shadows(
    session: Session,
    remote_collections: list[RemoteCollection],
    *,
    apply: bool,
    subject_id: int | None = None,
) -> BootstrapResult:
    remote_map = {item.subject_id: item for item in remote_collections}
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
    rows = session.execute(statement).all()
    local_subject_ids: set[int] = set()
    items: list[BootstrapItem] = []

    for work, identity in rows:
        local_subject_ids.add(identity.subject_id)
        if session.get(SyncShadow, identity.subject_id) is not None:
            items.append(BootstrapItem(identity.subject_id, work.title, "already_shadowed"))
            continue
        remote_record = remote_map.get(identity.subject_id)
        if remote_record is None:
            items.append(BootstrapItem(identity.subject_id, work.title, "remote_missing"))
            continue
        local = local_snapshot(session, identity.subject_id)
        remote = snapshot_from_remote(remote_record)
        if local != remote:
            changed_fields = tuple(
                field
                for field in CANONICAL_FIELDS
                if local.value_for(field) != remote.value_for(field)
            )
            items.append(
                BootstrapItem(
                    identity.subject_id,
                    work.title,
                    "bootstrap_mismatch",
                    changed_fields,
                )
            )
            continue
        outcome = "applied" if apply else "eligible"
        items.append(BootstrapItem(identity.subject_id, work.title, outcome))
        if apply:
            write_shadow(session, identity.subject_id, remote, remote_record.updated_at_utc)

    remote_only_ids = set(remote_map) - local_subject_ids
    if subject_id is not None:
        remote_only_ids &= {subject_id}
    return BootstrapResult(apply=apply, items=tuple(items), remote_only=len(remote_only_ids))

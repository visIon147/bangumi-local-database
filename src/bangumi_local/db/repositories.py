from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    GameProfile,
    SyncConflict,
    SyncShadow,
    Tag,
    Work,
    WorkLink,
    WorkTag,
)
from bangumi_local.domain.merge import CollectionDiff, DiffStatus
from bangumi_local.domain.models import RemoteCollection, SubjectType
from bangumi_local.domain.snapshots import CollectionSnapshot, normalized_tags


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def work_for_subject(session: Session, subject_id: int) -> Work | None:
    return session.scalar(
        select(Work)
        .join(BangumiSubject, BangumiSubject.work_id == Work.id)
        .where(BangumiSubject.subject_id == subject_id)
    )


def subject_for_work(session: Session, work_id: int) -> BangumiSubject | None:
    return session.scalar(select(BangumiSubject).where(BangumiSubject.work_id == work_id))


def upsert_work(session: Session, remote: RemoteCollection) -> tuple[Work, bool, bool]:
    identity = session.get(BangumiSubject, remote.subject_id)
    work = session.get(Work, identity.work_id) if identity is not None else None
    now = utc_now_iso()
    remote_subject = remote.subject
    metadata_values = {
        "kind": SubjectType(remote.subject_type).kind,
        "title": remote_subject.display_title,
        "title_cn": remote_subject.title_cn,
        "title_original": remote_subject.title_original,
        "summary": remote_subject.summary,
        "release_date": remote_subject.release_date,
        "cover_url": remote_subject.cover_url,
        # Compatibility bridge values are populated but never used for identity lookup.
        "bgm_subject_id": remote.subject_id,
        "bgm_url": f"https://bgm.tv/subject/{remote.subject_id}",
    }
    created = work is None
    if work is None:
        work = Work(created_at=now, updated_at=now, **metadata_values)
        session.add(work)
        session.flush()
        identity = BangumiSubject(
            subject_id=remote.subject_id,
            work_id=work.id,
            subject_type=int(remote.subject_type),
            url=f"https://bgm.tv/subject/{remote.subject_id}",
            metadata_available=remote_subject.metadata_available,
            last_observed_at=now,
        )
        session.add(identity)
        session.flush()
        if work.kind == "game":
            session.add(GameProfile(work_id=work.id))
        return work, True, True

    assert identity is not None
    identity.subject_type = int(remote.subject_type)
    identity.url = f"https://bgm.tv/subject/{remote.subject_id}"
    identity.metadata_available = remote_subject.metadata_available
    identity.last_observed_at = now
    values = metadata_values if remote_subject.metadata_available else {
        "kind": metadata_values["kind"],
        "bgm_subject_id": remote.subject_id,
        "bgm_url": metadata_values["bgm_url"],
    }
    changed = any(getattr(work, field) != value for field, value in values.items())
    if changed:
        for field, value in values.items():
            setattr(work, field, value)
        work.updated_at = now
    if work.kind == "game" and session.get(GameProfile, work.id) is None:
        session.add(GameProfile(work_id=work.id))
    return work, changed, created


def ensure_state(session: Session, subject_id: int) -> BangumiCollectionState:
    state = session.get(BangumiCollectionState, subject_id)
    if state is None:
        state = BangumiCollectionState(subject_id=subject_id, local_updated_at=utc_now_iso())
        session.add(state)
        session.flush()
    return state


def ensure_bangumi_link(session: Session, work: Work, subject_id: int) -> None:
    url = f"https://bgm.tv/subject/{subject_id}"
    link = session.scalar(
        select(WorkLink).where(
            WorkLink.work_id == work.id,
            WorkLink.source == "bangumi",
            WorkLink.url == url,
        )
    )
    if link is None:
        session.add(
            WorkLink(
                work_id=work.id,
                source="bangumi",
                url=url,
                external_id=str(subject_id),
                is_primary=True,
                match_source="bangumi_pull",
                match_confidence="confirmed",
                verified_at=utc_now_iso(),
            )
        )


def syncable_tag_names(session: Session, work_id: int) -> tuple[str, ...]:
    names = session.scalars(
        select(Tag.name)
        .join(WorkTag, WorkTag.tag_id == Tag.id)
        .where(WorkTag.work_id == work_id, Tag.sync_scope.in_(("bangumi", "both")))
    ).all()
    return normalized_tags(names)


def replace_bangumi_tags(session: Session, work_id: int, names: tuple[str, ...]) -> bool:
    desired = normalized_tags(names)
    if syncable_tag_names(session, work_id) == desired:
        return False
    syncable_links = session.scalars(
        select(WorkTag)
        .join(Tag, WorkTag.tag_id == Tag.id)
        .where(WorkTag.work_id == work_id, Tag.sync_scope.in_(("bangumi", "both")))
    ).all()
    for link in syncable_links:
        session.delete(link)
    session.flush()
    for name in desired:
        tag = session.scalar(select(Tag).where(Tag.name == name))
        if tag is None:
            tag = Tag(name=name, sync_scope="bangumi")
            session.add(tag)
            session.flush()
        elif tag.sync_scope == "local":
            tag.sync_scope = "both"
        if session.get(WorkTag, (work_id, tag.id)) is None:
            session.add(WorkTag(work_id=work_id, tag_id=tag.id, origin="bangumi"))
    return True


def local_snapshot(session: Session, subject_id: int) -> CollectionSnapshot:
    identity = session.get(BangumiSubject, subject_id)
    state = session.get(BangumiCollectionState, subject_id)
    if identity is None or state is None:
        raise LookupError(f"Bangumi collection state missing for subject {subject_id}")
    return CollectionSnapshot.create(
        collection_type=state.bgm_collection_type,
        rating=state.rating,
        comment=state.comment,
        is_private=state.is_private,
        tags=syncable_tag_names(session, identity.work_id),
    )


def apply_remote_snapshot(
    session: Session, subject_id: int, snapshot: CollectionSnapshot
) -> bool:
    before = local_snapshot(session, subject_id)
    if before == snapshot:
        return False
    return apply_remote_fields(session, subject_id, snapshot, ("type", "rate", "comment", "private", "tags"))


def apply_remote_fields(
    session: Session,
    subject_id: int,
    snapshot: CollectionSnapshot,
    fields: tuple[str, ...],
) -> bool:
    identity = session.get(BangumiSubject, subject_id)
    state = session.get(BangumiCollectionState, subject_id)
    if identity is None or state is None:
        raise LookupError(f"Bangumi collection state missing for subject {subject_id}")
    changed = False
    field_attributes = {
        "type": "bgm_collection_type",
        "rate": "rating",
        "comment": "comment",
        "private": "is_private",
    }
    values = {
        "type": snapshot.collection_type,
        "rate": snapshot.rating,
        "comment": snapshot.comment,
        "private": snapshot.is_private,
    }
    for field in fields:
        if field == "tags":
            changed = replace_bangumi_tags(session, identity.work_id, snapshot.tags) or changed
            continue
        attribute = field_attributes.get(field)
        if attribute is None:
            raise KeyError(f"Unknown canonical collection field: {field}")
        if getattr(state, attribute) != values[field]:
            setattr(state, attribute, values[field])
            changed = True
    if changed:
        state.local_updated_at = utc_now_iso()
    return changed


def write_shadow(
    session: Session,
    subject_id: int,
    snapshot: CollectionSnapshot,
    remote_updated_at: str,
) -> None:
    shadow = session.get(SyncShadow, subject_id)
    now = utc_now_iso()
    if shadow is None:
        session.add(
            SyncShadow(
                subject_id=subject_id,
                remote_snapshot_json=snapshot.to_json(),
                remote_hash=snapshot.digest(),
                remote_updated_at=remote_updated_at,
                synced_at=now,
            )
        )
        return
    shadow.remote_snapshot_json = snapshot.to_json()
    shadow.remote_hash = snapshot.digest()
    shadow.remote_updated_at = remote_updated_at
    shadow.synced_at = now


def shadow_snapshot(shadow: SyncShadow) -> CollectionSnapshot:
    return CollectionSnapshot.from_mapping(json.loads(shadow.remote_snapshot_json))


def _json_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def persist_conflicts(session: Session, subject_id: int, diff: CollectionDiff) -> int:
    conflict_fields = {
        field.field: (_json_value(field.base), _json_value(field.local), _json_value(field.remote))
        for field in diff.fields
        if field.status == DiffStatus.CONFLICT
    }
    existing = session.scalars(
        select(SyncConflict).where(
            SyncConflict.subject_id == subject_id, SyncConflict.status == "open"
        )
    ).all()
    existing_keys = {
        (item.field, item.base_json, item.local_json, item.remote_json): item for item in existing
    }
    current_keys = {
        (field, values[0], values[1], values[2]) for field, values in conflict_fields.items()
    }
    now = utc_now_iso()
    for key, item in existing_keys.items():
        if key not in current_keys:
            item.status = "resolved"
            item.resolution = "superseded_by_current_sync_state"
            item.resolved_at = now
    inserted = 0
    for field, (base_json, local_json, remote_json) in conflict_fields.items():
        key = (field, base_json, local_json, remote_json)
        if key in existing_keys:
            continue
        session.add(
            SyncConflict(
                subject_id=subject_id,
                field=field,
                base_json=base_json,
                local_json=local_json,
                remote_json=remote_json,
                status="open",
                created_at=now,
            )
        )
        inserted += 1
    return inserted


@dataclass(frozen=True, slots=True)
class CollectionListItem:
    subject_id: int
    subject_type: int
    kind: str
    title: str
    status: int | None
    rating: int | None
    tags: tuple[str, ...]
    bgm_url: str


def list_collections(
    session: Session, subject_type: SubjectType | None = None
) -> list[CollectionListItem]:
    statement = (
        select(Work, BangumiSubject, BangumiCollectionState)
        .join(BangumiSubject, BangumiSubject.work_id == Work.id)
        .join(BangumiCollectionState, BangumiCollectionState.subject_id == BangumiSubject.subject_id)
        .order_by(Work.title, Work.id)
    )
    if subject_type is not None:
        statement = statement.where(BangumiSubject.subject_type == int(subject_type))
    rows = session.execute(statement).all()
    return [
        CollectionListItem(
            subject_id=identity.subject_id,
            subject_type=identity.subject_type,
            kind=work.kind,
            title=work.title,
            status=state.bgm_collection_type,
            rating=state.rating,
            tags=syncable_tag_names(session, work.id),
            bgm_url=identity.url,
        )
        for work, identity, state in rows
    ]


# Compatibility names for Phase 1–3 callers.
upsert_game = upsert_work
list_collection_games = list_collections
GameListItem = CollectionListItem

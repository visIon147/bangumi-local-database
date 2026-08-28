from __future__ import annotations

from dataclasses import dataclass
import json
from math import ceil
from typing import Generic, TypeVar

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    ChangePlan,
    ChangePlanItem,
    DiscoveryCandidate,
    DiscoverySession,
    GameProfile,
    LibraryCollection,
    LibraryEntry,
    LibraryEntryCollection,
    PlanApplyRun,
    RatingQueueItem,
    RatingQueueSession,
    RemoteOperation,
    SyncConflict,
    SyncShadow,
    Tag,
    Work,
    WorkLink,
    WorkTag,
)
from bangumi_local.services.plans import load_plan


class ReadModelError(ValueError):
    pass


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    page: int
    page_size: int
    total: int

    @property
    def page_count(self) -> int:
        return ceil(self.total / self.page_size) if self.total else 0


@dataclass(frozen=True, slots=True)
class NamedCount:
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class DashboardView:
    work_count: int
    bangumi_collection_count: int
    steam_entry_count: int
    open_conflict_count: int
    actionable_plan_count: int
    active_rating_queue_count: int
    pending_rating_item_count: int
    active_discovery_session_count: int
    pending_discovery_candidate_count: int
    works_by_kind: tuple[NamedCount, ...]
    plans_by_status: tuple[NamedCount, ...]


@dataclass(frozen=True, slots=True)
class WorkSummary:
    work_id: int
    kind: str
    title: str
    title_cn: str | None
    title_original: str | None
    release_date: str | None
    cover_url: str | None
    subject_id: int | None
    subject_type: int | None
    bgm_url: str | None
    collection_status: int | None
    rating: int | None
    is_private: bool | None
    tags: tuple[str, ...]
    steam_app_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkTagView:
    name: str
    sync_scope: str
    namespace: str | None
    origin: str
    confidence: str | None


@dataclass(frozen=True, slots=True)
class WorkLinkView:
    source: str
    url: str
    external_id: str | None
    is_primary: bool
    match_source: str | None
    match_confidence: str | None
    verified_at: str | None


@dataclass(frozen=True, slots=True)
class BangumiCollectionView:
    subject_id: int
    subject_type: int
    url: str
    metadata_available: bool
    last_observed_at: str
    collection_status: int | None
    rating: int | None
    comment: str | None
    is_private: bool | None
    local_updated_at: str | None


@dataclass(frozen=True, slots=True)
class SteamEntryView:
    entry_id: int
    app_id: str
    title: str | None
    ownership_scope: str
    installed: bool | None
    playtime_minutes: int | None
    last_played_at: str | None
    match_status: str
    work_id: int | None
    cover_url: str | None
    collections: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GameProfileView:
    completion: str
    confidence: str
    playtime_minutes: int | None
    first_played_at: str | None
    last_played_at: str | None
    liked_aspects_json: str | None
    disliked_aspects_json: str | None
    has_private_notes: bool


@dataclass(frozen=True, slots=True)
class WorkDetail:
    work_id: int
    kind: str
    title: str
    title_cn: str | None
    title_original: str | None
    summary: str | None
    release_date: str | None
    cover_url: str | None
    created_at: str
    updated_at: str
    bangumi: BangumiCollectionView | None
    game_profile: GameProfileView | None
    tags: tuple[WorkTagView, ...]
    links: tuple[WorkLinkView, ...]
    steam_entries: tuple[SteamEntryView, ...]


@dataclass(frozen=True, slots=True)
class PlanSummary:
    plan_id: str
    format_version: int
    kind: str
    operation: str
    status: str
    created_by: str
    reverse_of_plan_id: str | None
    created_at: str
    reviewed_at: str | None
    applied_at: str | None
    planned_count: int
    unchanged_count: int


@dataclass(frozen=True, slots=True)
class PlanItemView:
    item_id: int
    work_id: int | None
    subject_id: int | None
    source_entry_id: int | None
    title: str
    bgm_url: str | None
    disposition: str
    reason: str
    item_status: str
    error: str | None
    changed_fields: tuple[str, ...]
    action_json: str
    selection_evidence_json: str
    before_snapshot_json: str | None
    intended_snapshot_json: str | None
    before_tags: tuple[str, ...]
    after_tags: tuple[str, ...] | None
    public_tags: tuple[str, ...]
    remote_existence: str | None


@dataclass(frozen=True, slots=True)
class PlanDetail:
    summary: PlanSummary
    selector_json: str
    summary_json: str
    content_hash: str
    tag: str | None
    old_tag: str | None
    new_tag: str | None
    items: tuple[PlanItemView, ...]


@dataclass(frozen=True, slots=True)
class ApplyRunView:
    run_id: str
    plan_id: str
    status: str
    started_at: str
    finished_at: str | None
    error: str | None
    backup_created: bool
    operation_count: int
    applied_operation_count: int


@dataclass(frozen=True, slots=True)
class RemoteOperationView:
    operation_id: str
    run_id: str
    plan_id: str
    plan_item_id: int
    work_id: int
    subject_id: int
    source: str
    request_method: str | None
    request_payload_json: str | None
    before_snapshot_json: str | None
    intended_snapshot_json: str | None
    actual_snapshot_json: str | None
    remote_existed_before: bool | None
    status: str
    attempt_count: int
    http_status: int | None
    error: str | None
    started_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class SteamSummary:
    entry_count: int
    collection_count: int
    installed_count: int
    categorized_count: int
    matched_count: int
    match_status_counts: tuple[NamedCount, ...]


@dataclass(frozen=True, slots=True)
class RatingQueueSummary:
    session_id: str
    status: str
    order_name: str
    random_seed: int | None
    cursor_position: int
    item_count: int
    created_at: str
    updated_at: str
    pending_count: int
    completed_count: int
    stale_count: int
    suppressed_count: int
    enrichment_failed_count: int


@dataclass(frozen=True, slots=True)
class DiscoverySessionSummary:
    session_id: str
    provider: str
    status: str
    cursor_position: int
    item_count: int
    created_at: str
    updated_at: str
    pending_count: int
    decided_count: int
    suppressed_count: int
    identity_conflict_count: int


def _validate_page(page: int, page_size: int) -> None:
    if page < 1:
        raise ReadModelError("page must be at least 1")
    if not 1 <= page_size <= 200:
        raise ReadModelError("page_size must be between 1 and 200")


def _loads_string_tuple(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    loaded = json.loads(value)
    if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
        raise ReadModelError("Stored JSON string list is invalid.")
    return tuple(loaded)


def dashboard(session: Session) -> DashboardView:
    works_by_kind = tuple(
        NamedCount(name, int(count))
        for name, count in session.execute(
            select(Work.kind, func.count(Work.id)).group_by(Work.kind).order_by(Work.kind)
        )
    )
    plans_by_status = tuple(
        NamedCount(name, int(count))
        for name, count in session.execute(
            select(ChangePlan.status, func.count(ChangePlan.id))
            .where(ChangePlan.archived_at.is_(None))
            .group_by(ChangePlan.status)
            .order_by(ChangePlan.status)
        )
    )
    return DashboardView(
        work_count=int(session.scalar(select(func.count()).select_from(Work)) or 0),
        bangumi_collection_count=int(
            session.scalar(select(func.count()).select_from(BangumiCollectionState)) or 0
        ),
        steam_entry_count=int(
            session.scalar(select(func.count()).select_from(LibraryEntry)) or 0
        ),
        open_conflict_count=int(
            session.scalar(
                select(func.count()).select_from(SyncConflict).where(SyncConflict.status == "open")
            )
            or 0
        ),
        actionable_plan_count=int(
            session.scalar(
                select(func.count())
                .select_from(ChangePlan)
                .where(
                    ChangePlan.status.in_(("draft", "reviewed", "partial")),
                    ChangePlan.archived_at.is_(None),
                )
            )
            or 0
        ),
        active_rating_queue_count=int(
            session.scalar(
                select(func.count())
                .select_from(RatingQueueSession)
                .where(RatingQueueSession.status == "active")
            )
            or 0
        ),
        pending_rating_item_count=int(
            session.scalar(
                select(func.count())
                .select_from(RatingQueueItem)
                .where(RatingQueueItem.item_status == "pending")
            )
            or 0
        ),
        active_discovery_session_count=int(
            session.scalar(
                select(func.count())
                .select_from(DiscoverySession)
                .where(DiscoverySession.status == "active")
            )
            or 0
        ),
        pending_discovery_candidate_count=int(
            session.scalar(
                select(func.count())
                .select_from(DiscoveryCandidate)
                .where(DiscoveryCandidate.item_status == "pending")
            )
            or 0
        ),
        works_by_kind=works_by_kind,
        plans_by_status=plans_by_status,
    )


def _work_tags(session: Session, work_ids: tuple[int, ...]) -> dict[int, tuple[str, ...]]:
    if not work_ids:
        return {}
    result: dict[int, list[str]] = {}
    for work_id, name in session.execute(
        select(WorkTag.work_id, Tag.name)
        .join(Tag, Tag.id == WorkTag.tag_id)
        .where(
            WorkTag.work_id.in_(work_ids),
            Tag.sync_scope.in_(("bangumi", "both")),
        )
        .order_by(WorkTag.work_id, Tag.name, Tag.id)
    ):
        result.setdefault(work_id, []).append(name)
    return {work_id: tuple(names) for work_id, names in result.items()}


def personal_tag_facets(session: Session) -> tuple[NamedCount, ...]:
    """Return exact Bangumi personal-tag names and their work counts."""

    return tuple(
        NamedCount(name, int(count))
        for name, count in session.execute(
            select(Tag.name, func.count(func.distinct(WorkTag.work_id)))
            .join(WorkTag, WorkTag.tag_id == Tag.id)
            .where(Tag.sync_scope.in_(("bangumi", "both")))
            .group_by(Tag.id, Tag.name)
            .order_by(func.lower(Tag.name), Tag.name, Tag.id)
        )
    )


def _steam_ids(session: Session, work_ids: tuple[int, ...]) -> dict[int, tuple[str, ...]]:
    if not work_ids:
        return {}
    result: dict[int, list[str]] = {}
    for work_id, app_id in session.execute(
        select(LibraryEntry.work_id, LibraryEntry.external_id)
        .where(LibraryEntry.work_id.in_(work_ids))
        .order_by(LibraryEntry.work_id, LibraryEntry.external_id)
    ):
        assert work_id is not None
        result.setdefault(work_id, []).append(app_id)
    return {work_id: tuple(app_ids) for work_id, app_ids in result.items()}


def list_works(
    session: Session,
    *,
    page: int = 1,
    page_size: int = 50,
    query: str | None = None,
    kinds: tuple[str, ...] = (),
    subject_types: tuple[int, ...] = (),
    collection_statuses: tuple[int, ...] = (),
    tags: tuple[str, ...] = (),
    tag_match: str = "all",
    exclude_tags: tuple[str, ...] = (),
    source: str | None = None,
    sort: str = "title-asc",
) -> Page[WorkSummary]:
    _validate_page(page, page_size)
    conditions = []
    normalized_query = (query or "").strip().casefold()
    if normalized_query:
        needle = f"%{normalized_query}%"
        conditions.append(
            or_(
                func.lower(Work.title).like(needle),
                func.lower(func.coalesce(Work.title_cn, "")).like(needle),
                func.lower(func.coalesce(Work.title_original, "")).like(needle),
            )
        )
    if kinds:
        conditions.append(Work.kind.in_(tuple(dict.fromkeys(kinds))))
    if subject_types:
        conditions.append(BangumiSubject.subject_type.in_(tuple(dict.fromkeys(subject_types))))
    if collection_statuses:
        conditions.append(
            BangumiCollectionState.bgm_collection_type.in_(
                tuple(dict.fromkeys(collection_statuses))
            )
        )
    normalized_tags = tuple(dict.fromkeys(tags))
    if tag_match not in {"all", "any"}:
        raise ReadModelError("tag_match must be all or any")
    tag_exists = tuple(
        exists(
            select(1)
            .select_from(WorkTag)
            .join(Tag, Tag.id == WorkTag.tag_id)
            .where(
                WorkTag.work_id == Work.id,
                Tag.name == tag,
                Tag.sync_scope.in_(("bangumi", "both")),
            )
        )
        for tag in normalized_tags
    )
    if tag_exists:
        conditions.extend(tag_exists if tag_match == "all" else (or_(*tag_exists),))
    normalized_excluded = tuple(dict.fromkeys(exclude_tags))
    if normalized_excluded:
        conditions.append(
            ~exists(
                select(1)
                .select_from(WorkTag)
                .join(Tag, Tag.id == WorkTag.tag_id)
                .where(
                    WorkTag.work_id == Work.id,
                    Tag.name.in_(normalized_excluded),
                    Tag.sync_scope.in_(("bangumi", "both")),
                )
            )
        )
    if source == "bangumi":
        conditions.append(BangumiSubject.subject_id.is_not(None))
    elif source == "steam":
        conditions.append(
            exists(select(1).select_from(LibraryEntry).where(LibraryEntry.work_id == Work.id))
        )
    elif source not in (None, "all"):
        raise ReadModelError("source must be all, bangumi, or steam")

    base = (
        select(Work, BangumiSubject, BangumiCollectionState, SyncShadow)
        .outerjoin(BangumiSubject, BangumiSubject.work_id == Work.id)
        .outerjoin(
            BangumiCollectionState,
            BangumiCollectionState.subject_id == BangumiSubject.subject_id,
        )
        .outerjoin(SyncShadow, SyncShadow.subject_id == BangumiSubject.subject_id)
    )
    if conditions:
        base = base.where(and_(*conditions))
    total = int(session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    sort_fields = {
        "title": func.lower(Work.title),
        "rating": BangumiCollectionState.rating,
        "release-date": Work.release_date,
        "collection-updated": SyncShadow.remote_updated_at,
        "local-updated": Work.updated_at,
    }
    try:
        sort_name, direction = sort.rsplit("-", 1)
        sort_column = sort_fields[sort_name]
    except (KeyError, ValueError):
        raise ReadModelError("Unsupported work sort.") from None
    if direction not in {"asc", "desc"}:
        raise ReadModelError("Unsupported work sort direction.")
    ordering = (
        sort_column.asc() if direction == "asc" else sort_column.desc()
    )
    rows = session.execute(
        base.order_by(case((sort_column.is_(None), 1), else_=0), ordering, Work.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    work_ids = tuple(work.id for work, _subject, _state, _shadow in rows)
    tags_by_work = _work_tags(session, work_ids)
    steam_by_work = _steam_ids(session, work_ids)
    items = tuple(
        WorkSummary(
            work_id=work.id,
            kind=work.kind,
            title=work.title,
            title_cn=work.title_cn,
            title_original=work.title_original,
            release_date=work.release_date,
            cover_url=work.cover_url,
            subject_id=subject.subject_id if subject else None,
            subject_type=subject.subject_type if subject else None,
            bgm_url=subject.url if subject else None,
            collection_status=state.bgm_collection_type if state else None,
            rating=state.rating if state else None,
            is_private=state.is_private if state else None,
            tags=tags_by_work.get(work.id, ()),
            steam_app_ids=steam_by_work.get(work.id, ()),
        )
        for work, subject, state, _shadow in rows
    )
    return Page(items, page, page_size, total)


def _steam_entry_views(session: Session, work_id: int) -> tuple[SteamEntryView, ...]:
    entries = list(
        session.scalars(
            select(LibraryEntry)
            .where(LibraryEntry.work_id == work_id)
            .order_by(LibraryEntry.external_id, LibraryEntry.id)
        ).all()
    )
    if not entries:
        return ()
    entry_ids = tuple(entry.id for entry in entries)
    collections: dict[int, list[str]] = {}
    for entry_id, name in session.execute(
        select(LibraryEntryCollection.library_entry_id, LibraryCollection.name)
        .join(LibraryCollection, LibraryCollection.id == LibraryEntryCollection.collection_id)
        .where(
            LibraryEntryCollection.library_entry_id.in_(entry_ids),
            LibraryEntryCollection.active.is_(True),
            LibraryCollection.active.is_(True),
        )
        .order_by(LibraryEntryCollection.library_entry_id, LibraryCollection.name)
    ):
        collections.setdefault(entry_id, []).append(name)
    cover_url = session.get(Work, work_id).cover_url  # type: ignore[union-attr]
    return tuple(
        SteamEntryView(
            entry_id=entry.id,
            app_id=entry.external_id,
            title=entry.title_observed,
            ownership_scope=entry.ownership_scope,
            installed=entry.installed,
            playtime_minutes=entry.playtime_minutes,
            last_played_at=entry.last_played_at,
            match_status=entry.match_status,
            work_id=entry.work_id,
            cover_url=cover_url,
            collections=tuple(collections.get(entry.id, ())),
        )
        for entry in entries
    )


def get_work_detail(session: Session, work_id: int) -> WorkDetail:
    work = session.get(Work, work_id)
    if work is None:
        raise ReadModelError(f"Work not found: {work_id}")
    subject = session.scalar(select(BangumiSubject).where(BangumiSubject.work_id == work_id))
    state = session.get(BangumiCollectionState, subject.subject_id) if subject else None
    profile = session.get(GameProfile, work_id)
    tag_views = tuple(
        WorkTagView(tag.name, tag.sync_scope, tag.namespace, work_tag.origin, work_tag.confidence)
        for work_tag, tag in session.execute(
            select(WorkTag, Tag)
            .join(Tag, Tag.id == WorkTag.tag_id)
            .where(WorkTag.work_id == work_id)
            .order_by(Tag.name, Tag.id)
        )
    )
    links = tuple(
        WorkLinkView(
            row.source, row.url, row.external_id, row.is_primary,
            row.match_source, row.match_confidence, row.verified_at,
        )
        for row in session.scalars(
            select(WorkLink)
            .where(WorkLink.work_id == work_id)
            .order_by(WorkLink.source, WorkLink.id)
        )
    )
    bangumi = (
        BangumiCollectionView(
            subject.subject_id,
            subject.subject_type,
            subject.url,
            subject.metadata_available,
            subject.last_observed_at,
            state.bgm_collection_type if state else None,
            state.rating if state else None,
            state.comment if state else None,
            state.is_private if state else None,
            state.local_updated_at if state else None,
        )
        if subject
        else None
    )
    game_profile = (
        GameProfileView(
            profile.completion,
            profile.confidence,
            profile.playtime_minutes,
            profile.first_played_at,
            profile.last_played_at,
            profile.liked_aspects_json,
            profile.disliked_aspects_json,
            bool(profile.notes_private),
        )
        if profile
        else None
    )
    return WorkDetail(
        work.id, work.kind, work.title, work.title_cn, work.title_original,
        work.summary, work.release_date, work.cover_url, work.created_at, work.updated_at,
        bangumi, game_profile, tag_views, links, _steam_entry_views(session, work_id),
    )


def _plan_counts(session: Session, plan_ids: tuple[str, ...]) -> dict[str, tuple[int, int]]:
    counts: dict[str, list[int]] = {plan_id: [0, 0] for plan_id in plan_ids}
    if not plan_ids:
        return {}
    for plan_id, disposition, count in session.execute(
        select(ChangePlanItem.plan_id, ChangePlanItem.disposition, func.count(ChangePlanItem.id))
        .where(ChangePlanItem.plan_id.in_(plan_ids))
        .group_by(ChangePlanItem.plan_id, ChangePlanItem.disposition)
    ):
        counts[plan_id][0 if disposition == "planned" else 1] = int(count)
    return {plan_id: (values[0], values[1]) for plan_id, values in counts.items()}


def _plan_summary(row: ChangePlan, counts: tuple[int, int]) -> PlanSummary:
    return PlanSummary(
        row.id, row.format_version, row.kind, row.operation, row.status, row.created_by,
        row.reverse_of_plan_id, row.created_at, row.reviewed_at, row.applied_at,
        counts[0], counts[1],
    )


def list_plans(
    session: Session,
    *,
    page: int = 1,
    page_size: int = 50,
    statuses: tuple[str, ...] = (),
    kinds: tuple[str, ...] = (),
) -> Page[PlanSummary]:
    _validate_page(page, page_size)
    statement = select(ChangePlan)
    if statuses:
        statement = statement.where(ChangePlan.status.in_(tuple(dict.fromkeys(statuses))))
    if kinds:
        statement = statement.where(ChangePlan.kind.in_(tuple(dict.fromkeys(kinds))))
    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = tuple(
        session.scalars(
            statement.order_by(ChangePlan.created_at.desc(), ChangePlan.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    counts = _plan_counts(session, tuple(row.id for row in rows))
    return Page(
        tuple(_plan_summary(row, counts.get(row.id, (0, 0))) for row in rows),
        page, page_size, total,
    )


def get_plan_detail(session: Session, plan_id: str) -> PlanDetail:
    stored = load_plan(session, plan_id, verify=True)
    row = stored.plan
    item_rows = tuple(
        session.scalars(
            select(ChangePlanItem)
            .where(ChangePlanItem.plan_id == plan_id)
            .order_by(ChangePlanItem.id)
        ).all()
    )
    items = tuple(
        PlanItemView(
            item.id, item.work_id, item.subject_id, item.source_entry_id,
            item.title, item.bgm_url, item.disposition, item.reason, item.item_status,
            item.error, _loads_string_tuple(item.changed_fields_json) or (),
            item.action_json, item.selection_evidence_json,
            item.before_snapshot_json, item.intended_snapshot_json,
            _loads_string_tuple(item.before_tags_json) or (),
            _loads_string_tuple(item.after_tags_json),
            _loads_string_tuple(item.public_tags_json) or (), item.remote_existence,
        )
        for item in item_rows
    )
    counts = _plan_counts(session, (plan_id,)).get(plan_id, (0, 0))
    return PlanDetail(
        _plan_summary(row, counts), row.selector_json, row.summary_json, row.content_hash,
        row.tag, row.old_tag, row.new_tag, items,
    )


def list_apply_runs(
    session: Session,
    *,
    page: int = 1,
    page_size: int = 50,
    plan_id: str | None = None,
    statuses: tuple[str, ...] = (),
) -> Page[ApplyRunView]:
    _validate_page(page, page_size)
    statement = select(PlanApplyRun)
    if plan_id is not None:
        statement = statement.where(PlanApplyRun.plan_id == plan_id)
    if statuses:
        statement = statement.where(PlanApplyRun.status.in_(tuple(dict.fromkeys(statuses))))
    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = tuple(
        session.scalars(
            statement.order_by(PlanApplyRun.started_at.desc(), PlanApplyRun.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    run_ids = tuple(row.id for row in rows)
    counts: dict[str, list[int]] = {run_id: [0, 0] for run_id in run_ids}
    if run_ids:
        for run_id, status, count in session.execute(
            select(RemoteOperation.run_id, RemoteOperation.status, func.count(RemoteOperation.id))
            .where(RemoteOperation.run_id.in_(run_ids))
            .group_by(RemoteOperation.run_id, RemoteOperation.status)
        ):
            counts[run_id][0] += int(count)
            if status == "applied":
                counts[run_id][1] += int(count)
    return Page(
        tuple(
            ApplyRunView(
                row.id, row.plan_id, row.status, row.started_at, row.finished_at, row.error,
                bool(row.backup_path), counts[row.id][0], counts[row.id][1],
            )
            for row in rows
        ),
        page, page_size, total,
    )


def list_remote_operations(
    session: Session,
    *,
    page: int = 1,
    page_size: int = 50,
    run_id: str | None = None,
    plan_id: str | None = None,
    statuses: tuple[str, ...] = (),
) -> Page[RemoteOperationView]:
    _validate_page(page, page_size)
    statement = select(RemoteOperation)
    if run_id is not None:
        statement = statement.where(RemoteOperation.run_id == run_id)
    if plan_id is not None:
        statement = statement.where(RemoteOperation.plan_id == plan_id)
    if statuses:
        statement = statement.where(RemoteOperation.status.in_(tuple(dict.fromkeys(statuses))))
    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = tuple(
        session.scalars(
            statement.order_by(RemoteOperation.started_at.desc(), RemoteOperation.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    return Page(
        tuple(
            RemoteOperationView(
                row.id, row.run_id, row.plan_id, row.plan_item_id, row.work_id,
                row.subject_id, row.source, row.request_method, row.request_payload_json,
                row.before_snapshot_json, row.intended_snapshot_json, row.actual_snapshot_json,
                row.remote_existed_before, row.status, row.attempt_count, row.http_status,
                row.error, row.started_at, row.finished_at,
            )
            for row in rows
        ),
        page, page_size, total,
    )


def steam_summary(session: Session) -> SteamSummary:
    status_counts = tuple(
        NamedCount(name, int(count))
        for name, count in session.execute(
            select(LibraryEntry.match_status, func.count(LibraryEntry.id))
            .group_by(LibraryEntry.match_status)
            .order_by(LibraryEntry.match_status)
        )
    )
    categorized = int(
        session.scalar(
            select(func.count(func.distinct(LibraryEntryCollection.library_entry_id))).where(
                LibraryEntryCollection.active.is_(True)
            )
        )
        or 0
    )
    return SteamSummary(
        entry_count=int(session.scalar(select(func.count()).select_from(LibraryEntry)) or 0),
        collection_count=int(
            session.scalar(
                select(func.count()).select_from(LibraryCollection).where(LibraryCollection.active.is_(True))
            )
            or 0
        ),
        installed_count=int(
            session.scalar(
                select(func.count()).select_from(LibraryEntry).where(LibraryEntry.installed.is_(True))
            )
            or 0
        ),
        categorized_count=categorized,
        matched_count=int(
            session.scalar(
                select(func.count())
                .select_from(LibraryEntry)
                .where(LibraryEntry.match_status == "confirmed")
            )
            or 0
        ),
        match_status_counts=status_counts,
    )


def list_rating_queue_summaries(
    session: Session,
    *,
    page: int = 1,
    page_size: int = 50,
    statuses: tuple[str, ...] = (),
) -> Page[RatingQueueSummary]:
    _validate_page(page, page_size)
    statement = select(RatingQueueSession)
    if statuses:
        statement = statement.where(RatingQueueSession.status.in_(tuple(dict.fromkeys(statuses))))
    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = tuple(
        session.scalars(
            statement.order_by(RatingQueueSession.created_at.desc(), RatingQueueSession.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    ids = tuple(row.id for row in rows)
    counts: dict[str, dict[str, int]] = {row.id: {} for row in rows}
    failed: dict[str, int] = {row.id: 0 for row in rows}
    if ids:
        for session_id, status, count in session.execute(
            select(RatingQueueItem.session_id, RatingQueueItem.item_status, func.count(RatingQueueItem.id))
            .where(RatingQueueItem.session_id.in_(ids))
            .group_by(RatingQueueItem.session_id, RatingQueueItem.item_status)
        ):
            counts[session_id][status] = int(count)
        for session_id, count in session.execute(
            select(RatingQueueItem.session_id, func.count(RatingQueueItem.id))
            .where(
                RatingQueueItem.session_id.in_(ids),
                RatingQueueItem.enrichment_status == "failed",
            )
            .group_by(RatingQueueItem.session_id)
        ):
            failed[session_id] = int(count)
    return Page(
        tuple(
            RatingQueueSummary(
                row.id, row.status, row.order_name, row.random_seed, row.cursor_position,
                row.item_count, row.created_at, row.updated_at,
                counts[row.id].get("pending", 0), counts[row.id].get("completed", 0),
                counts[row.id].get("stale", 0), counts[row.id].get("suppressed", 0),
                failed[row.id],
            )
            for row in rows
        ),
        page, page_size, total,
    )


def list_discovery_session_summaries(
    session: Session,
    *,
    page: int = 1,
    page_size: int = 50,
    statuses: tuple[str, ...] = (),
    providers: tuple[str, ...] = (),
) -> Page[DiscoverySessionSummary]:
    _validate_page(page, page_size)
    statement = select(DiscoverySession)
    if statuses:
        statement = statement.where(DiscoverySession.status.in_(tuple(dict.fromkeys(statuses))))
    if providers:
        statement = statement.where(DiscoverySession.provider.in_(tuple(dict.fromkeys(providers))))
    total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = tuple(
        session.scalars(
            statement.order_by(DiscoverySession.created_at.desc(), DiscoverySession.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    ids = tuple(row.id for row in rows)
    counts: dict[str, dict[str, int]] = {row.id: {} for row in rows}
    if ids:
        for session_id, status, count in session.execute(
            select(
                DiscoveryCandidate.session_id,
                DiscoveryCandidate.item_status,
                func.count(DiscoveryCandidate.id),
            )
            .where(DiscoveryCandidate.session_id.in_(ids))
            .group_by(DiscoveryCandidate.session_id, DiscoveryCandidate.item_status)
        ):
            counts[session_id][status] = int(count)
    return Page(
        tuple(
            DiscoverySessionSummary(
                row.id, row.provider, row.status, row.cursor_position, row.item_count,
                row.created_at, row.updated_at, counts[row.id].get("pending", 0),
                counts[row.id].get("decided", 0), counts[row.id].get("suppressed", 0),
                counts[row.id].get("identity_conflict", 0),
            )
            for row in rows
        ),
        page, page_size, total,
    )

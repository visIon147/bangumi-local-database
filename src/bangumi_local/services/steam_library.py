from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    LibraryCollection,
    LibraryEntry,
    LibraryEntryCollection,
    SourceAccount,
    Work,
)


class SteamLibraryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SteamCollectionListItem:
    external_id: str
    name: str
    kind: str
    active_entries: int
    active: bool


@dataclass(frozen=True, slots=True)
class SteamEntryListItem:
    app_id: str
    title: str | None
    collections: tuple[str, ...]
    ownership_scope: str
    installed: bool | None
    playtime_minutes: int | None
    match_status: str
    work_id: int | None
    bangumi_rating: int | None
    release_date: str | None
    last_played_at: str | None
    first_seen_at: str
    last_seen_at: str


def steam_account(session: Session, account_id: str | None = None) -> SourceAccount:
    statement = select(SourceAccount).where(SourceAccount.source == "steam")
    if account_id is not None:
        statement = statement.where(SourceAccount.external_account_id == account_id)
    accounts = session.scalars(statement.order_by(SourceAccount.id)).all()
    if not accounts:
        raise SteamLibraryError("No Steam import exists; run 'bld steam import --apply-local'.")
    if len(accounts) > 1 and account_id is None:
        raise SteamLibraryError("Multiple imported Steam accounts exist; configure an account ID.")
    return accounts[0]


def list_steam_collections(
    session: Session, account_id: str | None = None, *, sort: str = "name-asc"
) -> list[SteamCollectionListItem]:
    account = steam_account(session, account_id)
    count_memberships = (
        select(
            LibraryEntryCollection.collection_id,
            func.count(LibraryEntryCollection.library_entry_id).label("entry_count"),
        )
        .where(LibraryEntryCollection.active.is_(True))
        .group_by(LibraryEntryCollection.collection_id)
        .subquery()
    )
    rows = session.execute(
        select(LibraryCollection, count_memberships.c.entry_count)
        .outerjoin(
            count_memberships,
            count_memberships.c.collection_id == LibraryCollection.id,
        )
        .where(LibraryCollection.source_account_id == account.id)
        .order_by(LibraryCollection.active.desc(), LibraryCollection.name)
    ).all()
    result = [
        SteamCollectionListItem(
            external_id=collection.external_id,
            name=collection.name,
            kind=collection.kind,
            active_entries=int(count or 0),
            active=collection.active,
        )
        for collection, count in rows
    ]
    if sort not in {"name-asc", "name-desc", "count-asc", "count-desc"}:
        raise SteamLibraryError("Unsupported Steam collection sort.")
    key_name, direction = sort.rsplit("-", 1)
    key = (lambda item: item.name.casefold()) if key_name == "name" else (lambda item: item.active_entries)
    return sorted(result, key=lambda item: (key(item), item.external_id), reverse=direction == "desc")


def list_steam_entries(
    session: Session,
    *,
    account_id: str | None = None,
    collection_name: str | None = None,
    collection_regex: str | None = None,
    match_status: str | None = None,
    query: str | None = None,
    sort: str = "title-asc",
) -> list[SteamEntryListItem]:
    import re

    account = steam_account(session, account_id)
    entries = session.scalars(
        select(LibraryEntry)
        .where(LibraryEntry.source_account_id == account.id)
        .order_by(LibraryEntry.title_observed, LibraryEntry.external_id)
    ).all()
    memberships = session.execute(
        select(LibraryEntryCollection.library_entry_id, LibraryCollection.name)
        .join(LibraryCollection, LibraryCollection.id == LibraryEntryCollection.collection_id)
        .where(
            LibraryCollection.source_account_id == account.id,
            LibraryEntryCollection.active.is_(True),
            LibraryCollection.active.is_(True),
        )
    ).all()
    names_by_entry: dict[int, list[str]] = {}
    for entry_id, name in memberships:
        names_by_entry.setdefault(entry_id, []).append(name)
    pattern = re.compile(collection_regex) if collection_regex is not None else None
    normalized_query = (query or "").strip().casefold()
    work_ids = tuple(entry.work_id for entry in entries if entry.work_id is not None)
    work_data = {
        work.id: (work.release_date, state.rating if state else None)
        for work, _subject, state in session.execute(
            select(Work, BangumiSubject, BangumiCollectionState)
            .outerjoin(BangumiSubject, BangumiSubject.work_id == Work.id)
            .outerjoin(BangumiCollectionState, BangumiCollectionState.subject_id == BangumiSubject.subject_id)
            .where(Work.id.in_(work_ids))
        )
    } if work_ids else {}
    result: list[SteamEntryListItem] = []
    for entry in entries:
        names = tuple(sorted(names_by_entry.get(entry.id, ())))
        if collection_name is not None and collection_name not in names:
            continue
        if pattern is not None and not any(pattern.search(name) for name in names):
            continue
        if match_status is not None and entry.match_status != match_status:
            continue
        if normalized_query and normalized_query not in f"{entry.external_id} {entry.title_observed or ''}".casefold():
            continue
        release_date, rating = work_data.get(entry.work_id, (None, None))
        result.append(
            SteamEntryListItem(
                app_id=entry.external_id,
                title=entry.title_observed,
                collections=names,
                ownership_scope=entry.ownership_scope,
                installed=entry.installed,
                playtime_minutes=entry.playtime_minutes,
                match_status=entry.match_status,
                work_id=entry.work_id,
                bangumi_rating=rating,
                release_date=release_date,
                last_played_at=entry.last_played_at,
                first_seen_at=entry.first_seen_at,
                last_seen_at=entry.last_seen_at,
            )
        )
    sort_fields = {
        "title": lambda item: (item.title or "").casefold() or None,
        "rating": lambda item: item.bangumi_rating,
        "release-date": lambda item: item.release_date,
        "playtime": lambda item: item.playtime_minutes,
        "last-played": lambda item: item.last_played_at,
        "first-seen": lambda item: item.first_seen_at,
        "last-seen": lambda item: item.last_seen_at,
        "appid": lambda item: int(item.app_id),
        "match-status": lambda item: item.match_status,
    }
    try:
        sort_name, direction = sort.rsplit("-", 1)
        key = sort_fields[sort_name]
    except (KeyError, ValueError):
        raise SteamLibraryError("Unsupported Steam entry sort.") from None
    if direction not in {"asc", "desc"}:
        raise SteamLibraryError("Unsupported Steam entry sort direction.")
    non_null = [item for item in result if key(item) is not None]
    nulls = [item for item in result if key(item) is None]
    non_null.sort(key=lambda item: (key(item), int(item.app_id)), reverse=direction == "desc")
    nulls.sort(key=lambda item: int(item.app_id))
    return [*non_null, *nulls]

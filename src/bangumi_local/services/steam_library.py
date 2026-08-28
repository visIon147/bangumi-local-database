from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    LibraryCollection,
    LibraryEntry,
    LibraryEntryCollection,
    SourceAccount,
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
    session: Session, account_id: str | None = None
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
    return [
        SteamCollectionListItem(
            external_id=collection.external_id,
            name=collection.name,
            kind=collection.kind,
            active_entries=int(count or 0),
            active=collection.active,
        )
        for collection, count in rows
    ]


def list_steam_entries(
    session: Session,
    *,
    account_id: str | None = None,
    collection_name: str | None = None,
    collection_regex: str | None = None,
    match_status: str | None = None,
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
    result: list[SteamEntryListItem] = []
    for entry in entries:
        names = tuple(sorted(names_by_entry.get(entry.id, ())))
        if collection_name is not None and collection_name not in names:
            continue
        if pattern is not None and not any(pattern.search(name) for name in names):
            continue
        if match_status is not None and entry.match_status != match_status:
            continue
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
            )
        )
    return result

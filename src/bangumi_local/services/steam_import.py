from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from bangumi_local.adapters.steam import SteamSnapshot
from bangumi_local.db.models import (
    LibraryCollection,
    LibraryEntry,
    LibraryEntryCollection,
    LibraryImportRun,
    SourceAccount,
)
from bangumi_local.db.repositories import utc_now_iso
from bangumi_local.domain.plans import stable_json


@dataclass(frozen=True, slots=True)
class SteamImportSummary:
    source_kind: str
    entries_seen: int
    collections_seen: int
    manual_collections_seen: int
    categorized_entries: int
    manual_categorized_entries: int
    new_entries: int
    updated_entries: int
    new_collections: int
    updated_collections: int
    membership_changes: int
    applied: bool
    run_id: str | None = None


def preview_steam_import(session: Session, snapshot: SteamSnapshot) -> SteamImportSummary:
    account = session.scalar(
        select(SourceAccount).where(
            SourceAccount.source == "steam",
            SourceAccount.external_account_id == snapshot.account_id,
        )
    )
    existing_entries: dict[str, LibraryEntry] = {}
    existing_collections: dict[str, LibraryCollection] = {}
    if account is not None:
        existing_entries = {
            item.external_id: item
            for item in session.scalars(
                select(LibraryEntry).where(LibraryEntry.source_account_id == account.id)
            ).all()
        }
        existing_collections = {
            item.external_id: item
            for item in session.scalars(
                select(LibraryCollection).where(
                    LibraryCollection.source_account_id == account.id
                )
            ).all()
        }
    new_entries = sum(item.app_id not in existing_entries for item in snapshot.entries)
    updated_entries = sum(
        item.app_id in existing_entries
        and existing_entries[item.app_id].source_hash != item.source_hash
        for item in snapshot.entries
    )
    new_collections = sum(
        item.external_id not in existing_collections for item in snapshot.collections
    )
    updated_collections = sum(
        item.external_id in existing_collections
        and (
            existing_collections[item.external_id].name != item.name
            or existing_collections[item.external_id].kind != item.kind
            or not existing_collections[item.external_id].active
        )
        for item in snapshot.collections
    )
    intended_memberships = {
        (entry.app_id, collection_id)
        for entry in snapshot.entries
        for collection_id in entry.collection_ids
    }
    current_memberships: set[tuple[str, str]] = set()
    if account is not None:
        current_memberships = {
            (entry_id, collection_id)
            for entry_id, collection_id in session.execute(
                select(LibraryEntry.external_id, LibraryCollection.external_id)
                .join(
                    LibraryEntryCollection,
                    LibraryEntryCollection.library_entry_id == LibraryEntry.id,
                )
                .join(
                    LibraryCollection,
                    LibraryCollection.id == LibraryEntryCollection.collection_id,
                )
                .where(
                    LibraryEntry.source_account_id == account.id,
                    LibraryEntryCollection.active.is_(True),
                )
            )
        }
    membership_changes = (
        len(current_memberships.symmetric_difference(intended_memberships))
        if snapshot.category_snapshot_complete
        else len(intended_memberships - current_memberships)
    )
    manual_ids = {
        item.external_id for item in snapshot.collections if item.kind == "manual"
    }
    return SteamImportSummary(
        source_kind=snapshot.source_kind,
        entries_seen=len(snapshot.entries),
        collections_seen=len(snapshot.collections),
        manual_collections_seen=sum(item.kind == "manual" for item in snapshot.collections),
        categorized_entries=sum(bool(item.collection_ids) for item in snapshot.entries),
        manual_categorized_entries=sum(
            any(collection_id in manual_ids for collection_id in item.collection_ids)
            for item in snapshot.entries
        ),
        new_entries=new_entries,
        updated_entries=updated_entries,
        new_collections=new_collections,
        updated_collections=updated_collections,
        membership_changes=membership_changes,
        applied=False,
    )


def apply_steam_import(session: Session, snapshot: SteamSnapshot) -> SteamImportSummary:
    preview = preview_steam_import(session, snapshot)
    now = utc_now_iso()
    account = session.scalar(
        select(SourceAccount).where(
            SourceAccount.source == "steam",
            SourceAccount.external_account_id == snapshot.account_id,
        )
    )
    if account is None:
        account = SourceAccount(
            source="steam",
            external_account_id=snapshot.account_id,
            config_json=stable_json({"provider": snapshot.source_kind}),
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(account)
        session.flush()
    else:
        account.config_json = stable_json({"provider": snapshot.source_kind})
        account.last_seen_at = now

    run = LibraryImportRun(
        id=str(uuid4()),
        source_account_id=account.id,
        source_kind=snapshot.source_kind,
        snapshot_hash=snapshot.digest,
        counts_json="{}",
        status="running",
        started_at=now,
    )
    session.add(run)
    session.flush()

    collection_by_external: dict[str, LibraryCollection] = {}
    observed_collection_ids = {item.external_id for item in snapshot.collections}
    existing_collections = session.scalars(
        select(LibraryCollection).where(LibraryCollection.source_account_id == account.id)
    ).all()
    for collection in existing_collections:
        if snapshot.category_snapshot_complete and collection.external_id not in observed_collection_ids:
            collection.active = False
        collection_by_external[collection.external_id] = collection
    for record in snapshot.collections:
        collection = collection_by_external.get(record.external_id)
        if collection is None:
            collection = LibraryCollection(
                source_account_id=account.id,
                external_id=record.external_id,
                name=record.name,
                kind=record.kind,
                active=True,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(collection)
            session.flush()
            collection_by_external[record.external_id] = collection
        else:
            collection.name = record.name
            collection.kind = record.kind
            collection.active = True
            collection.last_seen_at = now

    entry_by_external = {
        item.external_id: item
        for item in session.scalars(
            select(LibraryEntry).where(LibraryEntry.source_account_id == account.id)
        ).all()
    }
    observed_entry_ids: set[int] = set()
    for record in snapshot.entries:
        entry = entry_by_external.get(record.app_id)
        localized_titles = dict(record.localized_titles)
        incoming_providers = [record.metadata_source] if record.metadata_source else []
        metadata: dict[str, object] = {"providers": incoming_providers}
        manual_title: str | None = None
        if entry is not None:
            try:
                existing_localized = json.loads(entry.localized_titles_json or "{}")
            except json.JSONDecodeError:
                existing_localized = {}
            if isinstance(existing_localized, dict):
                localized_titles = {
                    str(key): str(value)
                    for key, value in existing_localized.items()
                    if isinstance(key, str) and isinstance(value, str)
                } | localized_titles
            try:
                existing_metadata = json.loads(entry.metadata_json or "{}")
            except json.JSONDecodeError:
                existing_metadata = {}
            if isinstance(existing_metadata, dict):
                metadata = dict(existing_metadata)
                providers = metadata.get("providers")
                existing_providers = (
                    [str(item) for item in providers] if isinstance(providers, list) else []
                )
                metadata["providers"] = list(
                    dict.fromkeys((*existing_providers, *incoming_providers))
                )
                value = metadata.get("manual_title")
                manual_title = value.strip() if isinstance(value, str) and value.strip() else None
        metadata_json = stable_json(metadata)
        localized_json = stable_json(localized_titles)
        if entry is None:
            entry = LibraryEntry(
                source_account_id=account.id,
                external_id=record.app_id,
                title_observed=record.title,
                localized_titles_json=localized_json,
                ownership_scope=record.ownership_scope,
                installed=record.installed,
                playtime_minutes=record.playtime_minutes,
                last_played_at=record.last_played_at,
                metadata_source=record.metadata_source,
                metadata_json=metadata_json,
                source_hash=record.source_hash,
                match_status="unmatched",
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(entry)
            session.flush()
            entry_by_external[record.app_id] = entry
        else:
            entry.title_observed = manual_title or record.title or entry.title_observed
            entry.localized_titles_json = localized_json
            entry.ownership_scope = record.ownership_scope
            entry.installed = record.installed
            entry.playtime_minutes = record.playtime_minutes
            entry.last_played_at = record.last_played_at
            entry.metadata_source = record.metadata_source
            entry.metadata_json = metadata_json
            entry.source_hash = record.source_hash
            entry.last_seen_at = now
        observed_entry_ids.add(entry.id)

    intended_memberships = {
        (entry_by_external[record.app_id].id, collection_by_external[collection_id].id)
        for record in snapshot.entries
        for collection_id in record.collection_ids
    }
    memberships = session.scalars(
        select(LibraryEntryCollection)
        .join(LibraryEntry, LibraryEntry.id == LibraryEntryCollection.library_entry_id)
        .where(LibraryEntry.source_account_id == account.id)
    ).all()
    membership_by_key = {
        (item.library_entry_id, item.collection_id): item for item in memberships
    }
    membership_changes = 0
    if snapshot.category_snapshot_complete:
        for key, membership in membership_by_key.items():
            if membership.active and key not in intended_memberships:
                membership.active = False
                membership_changes += 1
    for record in snapshot.entries:
        entry = entry_by_external[record.app_id]
        for external_collection_id in record.collection_ids:
            collection = collection_by_external[external_collection_id]
            membership = membership_by_key.get((entry.id, collection.id))
            if membership is None:
                membership = LibraryEntryCollection(
                    library_entry_id=entry.id,
                    collection_id=collection.id,
                    active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(membership)
                membership_by_key[(entry.id, collection.id)] = membership
                membership_changes += 1
            else:
                if not membership.active:
                    membership_changes += 1
                membership.active = True
                membership.last_seen_at = now

    summary = SteamImportSummary(
        **{
            **asdict(preview),
            "membership_changes": membership_changes,
            "applied": True,
            "run_id": run.id,
        }
    )
    run.status = "applied"
    run.finished_at = utc_now_iso()
    run.counts_json = json.dumps(asdict(summary), ensure_ascii=False, sort_keys=True)
    return summary

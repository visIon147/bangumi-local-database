from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from bangumi_local.adapters.steam import SteamDataError, fetch_store_titles
from bangumi_local.db.models import LibraryEntry
from bangumi_local.db.repositories import utc_now_iso
from bangumi_local.domain.plans import stable_json
from bangumi_local.services.steam_library import SteamLibraryError, steam_account


class SteamTitleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedSteamTitle:
    entry_id: int
    app_id: str
    precondition_hash: str


@dataclass(frozen=True, slots=True)
class FetchedSteamTitle:
    prepared: PreparedSteamTitle
    localized_titles: tuple[tuple[str, str], ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SteamTitleCompletionResult:
    selected: int
    updated: int
    preserved_manual: int
    unavailable: int
    stale: int
    entries: tuple[dict[str, object], ...]


def _metadata(entry: LibraryEntry) -> dict[str, object]:
    try:
        value = json.loads(entry.metadata_json or "{}")
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) else {}


def _precondition(entry: LibraryEntry) -> str:
    payload = {
        "entry_id": entry.id,
        "app_id": entry.external_id,
        "source_hash": entry.source_hash,
        "title_observed": entry.title_observed,
        "localized_titles_json": entry.localized_titles_json,
        "metadata_json": entry.metadata_json,
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def prepare_title_completion(
    session: Session,
    *,
    account_id: str | None,
    app_ids: tuple[str, ...] | None,
    all_missing: bool,
    max_items: int = 250,
) -> tuple[PreparedSteamTitle, ...]:
    if (app_ids is None) == (not all_missing):
        raise SteamTitleError("Choose exactly one of appids or all-missing.")
    if not 1 <= max_items <= 250:
        raise SteamTitleError("Steam title completion max-items must be between 1 and 250.")
    account = steam_account(session, account_id)
    statement = select(LibraryEntry).where(LibraryEntry.source_account_id == account.id)
    if app_ids is not None:
        if not app_ids or any(not value.isdigit() for value in app_ids):
            raise SteamTitleError("Steam AppIDs must be numeric.")
        if len(app_ids) > 250:
            raise SteamTitleError("At most 250 Steam AppIDs can be completed at once.")
        if len(app_ids) > max_items:
            raise SteamTitleError(
                f"The explicit selection has {len(app_ids)} entries, exceeding max-items {max_items}."
            )
        statement = statement.where(LibraryEntry.external_id.in_(app_ids))
    else:
        statement = statement.where(
            (LibraryEntry.title_observed.is_(None)) | (LibraryEntry.title_observed == "")
        )
    entries = list(
        session.scalars(statement.order_by(LibraryEntry.external_id)).all()
    )
    if app_ids is not None:
        found = {entry.external_id for entry in entries}
        missing = [value for value in app_ids if value not in found]
        if missing:
            raise SteamTitleError(
                f"Steam AppID(s) have not been imported: {', '.join(missing)}"
            )
    elif len(entries) > max_items:
        raise SteamTitleError(
            f"{len(entries)} entries need titles; select AppIDs or raise a stable batch no larger than 250."
        )
    entries = entries[:max_items]
    return tuple(
        PreparedSteamTitle(entry.id, entry.external_id, _precondition(entry))
        for entry in entries
    )


def fetch_title_completion(
    prepared: tuple[PreparedSteamTitle, ...],
    *,
    timeout_seconds: float,
    request_delay_seconds: float = 0.25,
    fetch_fn: Callable[..., dict[str, str]] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[FetchedSteamTitle, ...]:
    if request_delay_seconds < 0:
        raise SteamTitleError("Steam title request delay must not be negative.")
    title_fetcher = fetch_fn or fetch_store_titles
    results: list[FetchedSteamTitle] = []
    for index, item in enumerate(prepared):
        if index and request_delay_seconds:
            sleep_fn(request_delay_seconds)
        try:
            titles = title_fetcher(item.app_id, timeout_seconds=timeout_seconds)
        except SteamDataError:
            results.append(FetchedSteamTitle(item, (), "steam_store_read_failed"))
            continue
        cleaned = tuple(
            sorted(
                (str(language), str(title).strip())
                for language, title in titles.items()
                if str(title).strip()
            )
        )
        results.append(
            FetchedSteamTitle(
                item,
                cleaned,
                None if cleaned else "steam_store_title_unavailable",
            )
        )
    return tuple(results)


def persist_title_completion(
    session: Session, fetched: tuple[FetchedSteamTitle, ...]
) -> SteamTitleCompletionResult:
    updated = preserved = unavailable = stale = 0
    details: list[dict[str, object]] = []
    for result in fetched:
        entry = session.get(LibraryEntry, result.prepared.entry_id)
        if entry is None or _precondition(entry) != result.prepared.precondition_hash:
            stale += 1
            details.append({"app_id": result.prepared.app_id, "status": "stale"})
            continue
        metadata = _metadata(entry)
        manual_title = metadata.get("manual_title")
        if isinstance(manual_title, str) and manual_title.strip():
            entry.title_observed = manual_title.strip()
            preserved += 1
            details.append(
                {
                    "app_id": entry.external_id,
                    "title": entry.title_observed,
                    "status": "manual_preserved",
                }
            )
            continue
        if result.error or not result.localized_titles:
            unavailable += 1
            details.append(
                {
                    "app_id": entry.external_id,
                    "status": "unavailable",
                    "reason": result.error or "steam_store_title_unavailable",
                }
            )
            continue
        try:
            localized = json.loads(entry.localized_titles_json or "{}")
        except json.JSONDecodeError:
            localized = {}
        if not isinstance(localized, dict):
            localized = {}
        localized.update(dict(result.localized_titles))
        preferred = (
            localized.get("english")
            or localized.get("schinese")
            or localized.get("japanese")
            or next(iter(localized.values()), None)
        )
        entry.localized_titles_json = stable_json(localized)
        entry.title_observed = str(preferred).strip() if preferred else entry.title_observed
        entry.metadata_source = "steam_store_page"
        providers = metadata.get("providers")
        provider_list = [str(item) for item in providers] if isinstance(providers, list) else []
        if "steam_store_page" not in provider_list:
            provider_list.append("steam_store_page")
        metadata["providers"] = provider_list
        metadata["title_source"] = "steam_store_page"
        metadata["title_observed_at"] = utc_now_iso()
        entry.metadata_json = stable_json(metadata)
        updated += 1
        details.append(
            {
                "app_id": entry.external_id,
                "title": entry.title_observed,
                "languages": sorted(localized),
                "status": "updated",
            }
        )
    return SteamTitleCompletionResult(
        selected=len(fetched),
        updated=updated,
        preserved_manual=preserved,
        unavailable=unavailable,
        stale=stale,
        entries=tuple(details),
    )


def set_manual_title(
    session: Session, *, account_id: str | None, app_id: str, title: str
) -> LibraryEntry:
    if not app_id.isdigit():
        raise SteamTitleError("Steam AppID must be numeric.")
    normalized = title.strip()
    if not normalized or len(normalized) > 500 or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        raise SteamTitleError("Manual title must be 1-500 characters without controls or newlines.")
    account = steam_account(session, account_id)
    entry = session.scalar(
        select(LibraryEntry).where(
            LibraryEntry.source_account_id == account.id,
            LibraryEntry.external_id == app_id,
        )
    )
    if entry is None:
        raise SteamTitleError(f"Steam AppID {app_id} has not been imported.")
    metadata = _metadata(entry)
    metadata["manual_title"] = normalized
    metadata["manual_title_updated_at"] = utc_now_iso()
    entry.metadata_json = stable_json(metadata)
    entry.title_observed = normalized
    return entry


def clear_manual_title(
    session: Session, *, account_id: str | None, app_id: str
) -> LibraryEntry:
    account = steam_account(session, account_id)
    entry = session.scalar(
        select(LibraryEntry).where(
            LibraryEntry.source_account_id == account.id,
            LibraryEntry.external_id == app_id,
        )
    )
    if entry is None:
        raise SteamTitleError(f"Steam AppID {app_id} has not been imported.")
    metadata = _metadata(entry)
    metadata.pop("manual_title", None)
    metadata.pop("manual_title_updated_at", None)
    entry.metadata_json = stable_json(metadata)
    return entry

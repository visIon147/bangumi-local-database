from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import httpx
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from bangumi_local.config import Settings
from bangumi_local.domain.plans import stable_json


class SteamDataError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SteamCollectionRecord:
    external_id: str
    name: str
    kind: str
    app_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SteamEntryRecord:
    app_id: str
    title: str | None = None
    localized_titles: tuple[tuple[str, str], ...] = ()
    ownership_scope: str = "unknown"
    installed: bool | None = None
    playtime_minutes: int | None = None
    last_played_at: str | None = None
    metadata_source: str | None = None
    collection_ids: tuple[str, ...] = ()

    def as_hash_dict(self) -> dict[str, object]:
        return {
            "app_id": self.app_id,
            "title": self.title,
            "localized_titles": dict(self.localized_titles),
            "ownership_scope": self.ownership_scope,
            "installed": self.installed,
            "playtime_minutes": self.playtime_minutes,
            "last_played_at": self.last_played_at,
            "metadata_source": self.metadata_source,
            "collection_ids": list(self.collection_ids),
        }

    @property
    def source_hash(self) -> str:
        return hashlib.sha256(stable_json(self.as_hash_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SteamSnapshot:
    account_id: str
    source_kind: str
    collections: tuple[SteamCollectionRecord, ...]
    entries: tuple[SteamEntryRecord, ...]
    category_snapshot_complete: bool

    @property
    def digest(self) -> str:
        payload = {
            "account_id": self.account_id,
            "source_kind": self.source_kind,
            "category_snapshot_complete": self.category_snapshot_complete,
            "collections": [
                {
                    "external_id": item.external_id,
                    "name": item.name,
                    "kind": item.kind,
                    "app_ids": list(item.app_ids),
                }
                for item in self.collections
            ],
            "entries": [item.as_hash_dict() for item in self.entries],
        }
        return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SteamDetection:
    root: Path
    account_ids: tuple[str, ...]
    selected_account_id: str
    category_source: str
    category_file_available: bool
    legacy_file_available: bool
    local_config_available: bool
    installed_manifest_count: int


_TOKEN = re.compile(r'"((?:\\.|[^"\\])*)"|([{}])')


def _unescape_vdf(value: str) -> str:
    return value.replace(r"\"", '"').replace(r"\\", "\\")


def parse_text_vdf(text: str) -> dict[str, Any]:
    tokens: list[str] = []
    for match in _TOKEN.finditer(text):
        tokens.append(match.group(2) or _unescape_vdf(match.group(1)))
    index = 0

    def parse_object(expect_close: bool) -> dict[str, Any]:
        nonlocal index
        result: dict[str, Any] = {}
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if token == "}":
                if not expect_close:
                    raise SteamDataError("Unexpected closing brace in Steam VDF.")
                return result
            if token == "{":
                raise SteamDataError("Unexpected opening brace in Steam VDF.")
            key = token
            if index >= len(tokens):
                raise SteamDataError("Missing value in Steam VDF.")
            value = tokens[index]
            index += 1
            if value == "{":
                result[key] = parse_object(True)
            elif value == "}":
                raise SteamDataError("Missing value before closing Steam VDF block.")
            else:
                result[key] = value
        if expect_close:
            raise SteamDataError("Unterminated Steam VDF block.")
        return result

    parsed = parse_object(False)
    if index != len(tokens):
        raise SteamDataError("Trailing Steam VDF tokens.")
    return parsed


def _read_vdf(path: Path, role: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
        return parse_text_vdf(raw)
    except (OSError, UnicodeError, SteamDataError) as exc:
        raise SteamDataError(f"Could not read {role}: {exc}") from exc


def _mapping_value(mapping: dict[str, Any], key: str) -> Any:
    target = key.casefold()
    for candidate, value in mapping.items():
        if candidate.casefold() == target:
            return value
    return None


def _nested(mapping: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = _mapping_value(current, key)
    return current if isinstance(current, dict) else {}


def _windows_registry_root() -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _ = winreg.QueryValueEx(key, "SteamPath")
        return Path(str(value))
    except (FileNotFoundError, OSError):
        return None


def detect_steam_root(explicit: Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    registry = _windows_registry_root()
    if registry is not None:
        candidates.append(registry)
    if sys.platform == "darwin":
        candidates.append(Path.home() / "Library/Application Support/Steam")
    else:
        candidates.extend((Path.home() / ".steam/steam", Path.home() / ".local/share/Steam"))
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if (resolved / "userdata").is_dir():
            return resolved
    raise SteamDataError("Steam root was not found; set BLD_STEAM_ROOT.")


def _account_ids(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            (item.name for item in (root / "userdata").iterdir() if item.is_dir() and item.name.isdigit()),
            key=int,
        )
    )


def select_account(root: Path, explicit: str | None) -> str:
    accounts = _account_ids(root)
    if explicit:
        selected = explicit.strip()
        if selected not in accounts:
            raise SteamDataError("Configured Steam account was not found under userdata.")
        return selected
    if len(accounts) == 1:
        return accounts[0]
    if not accounts:
        raise SteamDataError("No numeric Steam userdata account directory was found.")
    raise SteamDataError("Multiple Steam accounts were found; set BLD_STEAM_ACCOUNT_ID.")


def _cloud_collections(path: Path) -> tuple[SteamCollectionRecord, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SteamDataError(f"Could not read Steam cloud collection cache: {exc}") from exc
    if not isinstance(payload, list):
        raise SteamDataError("Steam cloud collection cache has an unexpected root value.")
    collections: list[SteamCollectionRecord] = []
    for pair in payload:
        if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
            continue
        key, descriptor = pair
        if not key.startswith("user-collections.") or not isinstance(descriptor, dict):
            continue
        raw_value = descriptor.get("value")
        if not isinstance(raw_value, str):
            continue
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        external_id = str(value.get("id") or key.removeprefix("user-collections."))
        suffix = key.removeprefix("user-collections.")
        kind = "manual" if suffix.startswith("uc-") else suffix
        name = str(value.get("name") or suffix).strip()
        added = {str(item) for item in value.get("added", []) if str(item).isdigit()}
        removed = {str(item) for item in value.get("removed", []) if str(item).isdigit()}
        if name:
            collections.append(
                SteamCollectionRecord(external_id, name, kind, tuple(sorted(added - removed, key=int)))
            )
    if not any(item.kind == "manual" for item in collections):
        raise SteamDataError("Steam cloud cache contained no custom collection records.")
    return tuple(sorted(collections, key=lambda item: (item.kind, item.name, item.external_id)))


def _legacy_collections(path: Path) -> tuple[SteamCollectionRecord, ...]:
    payload = _read_vdf(path, "legacy Steam shared config")
    apps = _nested(payload, "UserRoamingConfigStore", "Software", "Valve", "Steam", "Apps")
    by_name: dict[str, set[str]] = {}
    for app_id, values in apps.items():
        if not app_id.isdigit() or not isinstance(values, dict):
            continue
        tags = _mapping_value(values, "tags")
        if not isinstance(tags, dict):
            continue
        for name in tags.values():
            normalized = str(name).strip()
            if normalized:
                by_name.setdefault(normalized, set()).add(app_id)
    if not by_name:
        raise SteamDataError("Legacy Steam shared config contained no collection tags.")
    return tuple(
        SteamCollectionRecord(
            external_id=f"legacy:{name}",
            name=name,
            kind="favorite" if name.casefold() == "favorite" else "manual",
            app_ids=tuple(sorted(app_ids, key=int)),
        )
        for name, app_ids in sorted(by_name.items())
    )


def _unix_iso(raw: object) -> str | None:
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _local_config_entries(path: Path) -> dict[str, SteamEntryRecord]:
    if not path.exists():
        return {}
    payload = _read_vdf(path, "Steam local config")
    apps = _nested(payload, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps")
    result: dict[str, SteamEntryRecord] = {}
    for app_id, values in apps.items():
        if not app_id.isdigit() or not isinstance(values, dict):
            continue
        name = _mapping_value(values, "name")
        playtime = _mapping_value(values, "Playtime")
        try:
            minutes = int(playtime) if playtime is not None else None
        except (TypeError, ValueError):
            minutes = None
        result[app_id] = SteamEntryRecord(
            app_id=app_id,
            title=str(name).strip() if name else None,
            ownership_scope="visible",
            playtime_minutes=minutes,
            last_played_at=_unix_iso(_mapping_value(values, "LastPlayed")),
            metadata_source="steam_localconfig",
        )
    return result


def _installed_entries(root: Path) -> dict[str, SteamEntryRecord]:
    library_file = root / "steamapps/libraryfolders.vdf"
    library_roots = {root}
    if library_file.exists():
        payload = _read_vdf(library_file, "Steam library folders")
        folders = _mapping_value(payload, "libraryfolders")
        if isinstance(folders, dict):
            for key, value in folders.items():
                if not key.isdigit() or not isinstance(value, dict):
                    continue
                raw_path = _mapping_value(value, "path")
                if raw_path:
                    library_roots.add(Path(str(raw_path)).expanduser().resolve())
    result: dict[str, SteamEntryRecord] = {}
    for library_root in library_roots:
        for manifest in (library_root / "steamapps").glob("appmanifest_*.acf"):
            try:
                app_state = _mapping_value(_read_vdf(manifest, "Steam app manifest"), "AppState")
            except SteamDataError:
                continue
            if not isinstance(app_state, dict):
                continue
            app_id = str(_mapping_value(app_state, "appid") or "")
            if not app_id.isdigit():
                continue
            raw_name = _mapping_value(app_state, "name")
            result[app_id] = SteamEntryRecord(
                app_id=app_id,
                title=str(raw_name).strip() if raw_name else None,
                ownership_scope="installed",
                installed=True,
                metadata_source="steam_appmanifest",
            )
    return result


def _merge_entry(base: SteamEntryRecord, incoming: SteamEntryRecord) -> SteamEntryRecord:
    scope_priority = {"unknown": 0, "installed": 1, "categorized": 2, "visible": 3, "owned": 4}
    return SteamEntryRecord(
        app_id=base.app_id,
        title=incoming.title or base.title,
        localized_titles=incoming.localized_titles or base.localized_titles,
        ownership_scope=(
            incoming.ownership_scope
            if scope_priority[incoming.ownership_scope] > scope_priority[base.ownership_scope]
            else base.ownership_scope
        ),
        installed=incoming.installed if incoming.installed is not None else base.installed,
        playtime_minutes=(
            incoming.playtime_minutes
            if incoming.playtime_minutes is not None
            else base.playtime_minutes
        ),
        last_played_at=incoming.last_played_at or base.last_played_at,
        metadata_source=incoming.metadata_source or base.metadata_source,
        collection_ids=base.collection_ids,
    )


def detect_steam(settings: Settings) -> SteamDetection:
    root = detect_steam_root(settings.steam_root)
    accounts = _account_ids(root)
    account = select_account(root, settings.steam_account_id)
    account_root = root / "userdata" / account
    cloud = account_root / "config/cloudstorage/cloud-storage-namespace-1.json"
    legacy = account_root / "7/remote/sharedconfig.vdf"
    source = "steam_cloudstorage" if cloud.exists() else "steam_sharedconfig"
    manifests = _installed_entries(root)
    return SteamDetection(
        root=root,
        account_ids=accounts,
        selected_account_id=account,
        category_source=source,
        category_file_available=cloud.exists(),
        legacy_file_available=legacy.exists(),
        local_config_available=(account_root / "config/localconfig.vdf").exists(),
        installed_manifest_count=len(manifests),
    )


def _owned_games(settings: Settings) -> dict[str, SteamEntryRecord]:
    if settings.steam_web_api_key is None or not settings.steam_id64:
        raise SteamDataError(
            "Network Steam import requires STEAM_WEB_API_KEY and BLD_STEAM_ID64."
        )
    try:
        response = httpx.get(
            "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/",
            params={
                "key": settings.steam_web_api_key.get_secret_value(),
                "steamid": settings.steam_id64,
                "include_appinfo": "true",
                "include_played_free_games": "true",
                "format": "json",
            },
            timeout=settings.bangumi_request_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise SteamDataError("Steam Web API request failed.") from exc
    if not response.is_success:
        raise SteamDataError(f"Steam Web API returned HTTP {response.status_code}.")
    try:
        games = response.json().get("response", {}).get("games", [])
    except (ValueError, AttributeError) as exc:
        raise SteamDataError("Steam Web API returned invalid JSON.") from exc
    result: dict[str, SteamEntryRecord] = {}
    for game in games:
        app_id = str(game.get("appid", ""))
        if not app_id.isdigit():
            continue
        result[app_id] = SteamEntryRecord(
            app_id=app_id,
            title=str(game.get("name") or "").strip() or None,
            ownership_scope="owned",
            playtime_minutes=int(game.get("playtime_forever") or 0),
            last_played_at=_unix_iso(game.get("rtime_last_played")),
            metadata_source="steam_webapi",
        )
    return result


def fetch_store_titles(app_id: str, *, timeout_seconds: float = 20.0) -> dict[str, str]:
    if not app_id.isdigit():
        raise SteamDataError("Steam AppID must be numeric.")
    titles: dict[str, str] = {}
    for language in ("english", "schinese", "japanese"):
        try:
            response = httpx.get(
                "https://store.steampowered.com/api/appdetails",
                params={"appids": app_id, "l": language},
                timeout=timeout_seconds,
            )
            payload = response.json()
            data = payload.get(app_id, {})
            title = data.get("data", {}).get("name") if data.get("success") else None
            if title:
                titles[language] = str(title).strip()
        except (httpx.HTTPError, ValueError, AttributeError):
            continue
    return titles


def read_steam_snapshot(settings: Settings, *, allow_network: bool = False) -> SteamSnapshot:
    detection = detect_steam(settings)
    account_root = detection.root / "userdata" / detection.selected_account_id
    cloud = account_root / "config/cloudstorage/cloud-storage-namespace-1.json"
    legacy = account_root / "7/remote/sharedconfig.vdf"
    collections: tuple[SteamCollectionRecord, ...]
    source_kind: str
    if cloud.exists():
        try:
            collections = _cloud_collections(cloud)
            source_kind = "steam_cloudstorage"
        except SteamDataError:
            if not legacy.exists():
                raise
            collections = _legacy_collections(legacy)
            source_kind = "steam_sharedconfig"
    elif legacy.exists():
        collections = _legacy_collections(legacy)
        source_kind = "steam_sharedconfig"
    else:
        raise SteamDataError("No supported Steam collection source was found.")

    collection_ids_by_app: dict[str, list[str]] = {}
    for collection in collections:
        for app_id in collection.app_ids:
            collection_ids_by_app.setdefault(app_id, []).append(collection.external_id)
    entries: dict[str, SteamEntryRecord] = {
        app_id: SteamEntryRecord(
            app_id=app_id,
            ownership_scope="categorized",
            collection_ids=tuple(sorted(ids)),
        )
        for app_id, ids in collection_ids_by_app.items()
    }
    local_entries = _local_config_entries(account_root / "config/localconfig.vdf")
    installed_entries = _installed_entries(detection.root)
    network_entries = _owned_games(settings) if allow_network else {}
    for app_id, incoming in (
        *local_entries.items(),
        *installed_entries.items(),
        *network_entries.items(),
    ):
        base = entries.get(app_id, SteamEntryRecord(app_id=app_id))
        merged = _merge_entry(base, incoming)
        entries[app_id] = replace(
            merged, collection_ids=tuple(sorted(collection_ids_by_app.get(app_id, ())))
        )
    return SteamSnapshot(
        account_id=detection.selected_account_id,
        source_kind=source_kind,
        collections=collections,
        entries=tuple(sorted(entries.values(), key=lambda item: int(item.app_id))),
        category_snapshot_complete=True,
    )

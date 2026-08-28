from __future__ import annotations

from collections.abc import Mapping, MutableMapping
import os
from pathlib import Path
import re
import stat
import tempfile

import tomlkit

from bangumi_local.domain.steam import (
    SteamRuleConfiguration,
    steam_rule_configuration_from_payload,
)


class SettingsEditError(ValueError):
    """A sanitized local configuration editing failure."""


_ENV_ASSIGNMENT = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=.*$"
)


def environment_managed(*names: str) -> bool:
    return any(name in os.environ for name in names)


def _validate_text(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SettingsEditError("Configuration values cannot contain control characters.")
    return value


def _dotenv_value(value: str) -> str:
    validated = _validate_text(value)
    return "'" + validated.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _atomic_text_replace(path: Path, content: str) -> None:
    parent = path.parent if str(path.parent) else Path(".")
    if not parent.exists() or not parent.is_dir():
        raise SettingsEditError("The local configuration directory does not exist.")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=parent,
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        else:
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except SettingsEditError:
        raise
    except OSError as exc:
        raise SettingsEditError("Could not update the local configuration file.") from exc
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def update_dotenv(path: Path, updates: Mapping[str, str | None]) -> None:
    """Update known dotenv keys while preserving comments and unknown lines."""

    normalized = {str(key): None if value is None else _validate_text(value) for key, value in updates.items()}
    try:
        original = path.read_text(encoding="utf-8") if path.exists() else ""
    except (OSError, UnicodeError) as exc:
        raise SettingsEditError("Could not read the local environment file.") from exc
    newline = "\r\n" if "\r\n" in original else "\n"
    output: list[str] = []
    handled: set[str] = set()
    for line in original.splitlines():
        match = _ENV_ASSIGNMENT.match(line)
        key = match.group("key") if match else None
        if key not in normalized:
            output.append(line)
            continue
        if key in handled:
            continue
        handled.add(key)
        value = normalized[key]
        if value is not None:
            output.append(f"{key}={_dotenv_value(value)}")
    for key, value in normalized.items():
        if key in handled or value is None:
            continue
        output.append(f"{key}={_dotenv_value(value)}")
    content = newline.join(output)
    if output:
        content += newline
    _atomic_text_replace(path, content)


def save_steam_rule_configuration(
    path: Path,
    payload: Mapping[str, object],
) -> SteamRuleConfiguration:
    """Round-trip update managed Steam rule fields in a local TOML file."""

    configuration = steam_rule_configuration_from_payload(payload)
    try:
        document = (
            tomlkit.parse(path.read_text(encoding="utf-8"))
            if path.exists()
            else tomlkit.document()
        )
    except (OSError, UnicodeError, tomlkit.exceptions.ParseError) as exc:
        raise SettingsEditError("Could not read the Steam rule configuration.") from exc
    steam = document.get("steam")
    if steam is None:
        steam = tomlkit.table()
        document["steam"] = steam
    if not isinstance(steam, MutableMapping):
        raise SettingsEditError("The Steam configuration section is invalid.")
    steam["allow_network"] = configuration.allow_network
    if configuration.remaining_status is None:
        steam.pop("remaining_status", None)
    else:
        steam["remaining_status"] = configuration.remaining_status.label
    rule_array = tomlkit.aot()
    for rule in configuration.rules:
        item = tomlkit.table()
        item["match"] = rule.match
        item["pattern"] = rule.pattern
        item["status"] = rule.status.label
        item["case_sensitive"] = rule.case_sensitive
        rule_array.append(item)
    steam["status_rules"] = rule_array
    _atomic_text_replace(path, tomlkit.dumps(document))
    return configuration

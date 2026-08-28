from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import re
import tomllib
from pathlib import Path
import unicodedata

from bangumi_local.domain.models import CollectionStatus


class SteamConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SteamStatusRule:
    match: str
    pattern: str
    status: CollectionStatus
    case_sensitive: bool = True

    def matches(self, collection_name: str) -> bool:
        candidate = unicodedata.normalize("NFKC", collection_name).strip()
        pattern = unicodedata.normalize("NFKC", self.pattern).strip()
        if not self.case_sensitive:
            candidate = candidate.casefold()
            pattern = pattern.casefold()
        if self.match == "exact":
            return candidate == pattern
        if self.match == "contains":
            return pattern in candidate
        if self.match == "regex":
            flags = 0 if self.case_sensitive else re.IGNORECASE
            return re.search(self.pattern, collection_name, flags=flags) is not None
        raise SteamConfigurationError(f"Unsupported Steam rule matcher: {self.match}")


@dataclass(frozen=True, slots=True)
class SteamRuleConfiguration:
    rules: tuple[SteamStatusRule, ...]
    remaining_status: CollectionStatus | None = None
    allow_network: bool = False


def _status(value: object) -> CollectionStatus:
    normalized = str(value).strip().lower().replace("_", "-")
    for status in CollectionStatus:
        if status.label == normalized:
            return status
    raise SteamConfigurationError(
        "Steam rule status must be wish, done, doing, on-hold, or dropped."
    )


def default_steam_rules() -> SteamRuleConfiguration:
    return SteamRuleConfiguration(
        rules=(
            SteamStatusRule("contains", "完结", CollectionStatus.DONE),
            SteamStatusRule("exact", "在用", CollectionStatus.DOING),
        )
    )


def steam_rule_configuration_from_payload(
    payload: Mapping[str, object],
) -> SteamRuleConfiguration:
    """Validate an untrusted UI/job rule payload into the canonical domain model."""

    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list):
        raise SteamConfigurationError("Steam rules must be a list.")
    rules: list[SteamStatusRule] = []
    for index, raw in enumerate(raw_rules):
        if not isinstance(raw, Mapping):
            raise SteamConfigurationError(f"Invalid steam.status_rules[{index}].")
        matcher = str(raw.get("match", "")).strip().lower()
        pattern = str(raw.get("pattern", "")).strip()
        case_sensitive = raw.get("case_sensitive", True)
        if matcher not in {"exact", "contains", "regex"} or not pattern:
            raise SteamConfigurationError(f"Invalid steam.status_rules[{index}].")
        if not isinstance(case_sensitive, bool):
            raise SteamConfigurationError(
                f"steam.status_rules[{index}].case_sensitive must be boolean."
            )
        if matcher == "regex":
            try:
                re.compile(pattern)
            except re.error as exc:
                raise SteamConfigurationError(
                    f"Invalid regex in steam.status_rules[{index}]: {exc}"
                ) from exc
        rules.append(
            SteamStatusRule(
                match=matcher,
                pattern=pattern,
                status=_status(raw.get("status")),
                case_sensitive=case_sensitive,
            )
        )
    if not rules:
        raise SteamConfigurationError("At least one Steam status rule is required.")
    raw_remaining = payload.get("remaining_status")
    remaining = _status(raw_remaining) if raw_remaining not in (None, "") else None
    allow_network = payload.get("allow_network", False)
    if not isinstance(allow_network, bool):
        raise SteamConfigurationError("steam.allow_network must be boolean.")
    return SteamRuleConfiguration(
        rules=tuple(rules),
        remaining_status=remaining,
        allow_network=allow_network,
    )


def load_steam_rules(path: Path) -> SteamRuleConfiguration:
    if not path.exists():
        return default_steam_rules()
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SteamConfigurationError(f"Could not read Steam rule configuration: {exc}") from exc
    steam = payload.get("steam", {})
    raw_rules = steam.get("status_rules", [])
    if not raw_rules:
        defaults = default_steam_rules()
        raw_rules = [
            {
                "match": rule.match,
                "pattern": rule.pattern,
                "status": rule.status.label,
                "case_sensitive": rule.case_sensitive,
            }
            for rule in defaults.rules
        ]
    return steam_rule_configuration_from_payload(
        {
            "rules": raw_rules,
            "remaining_status": steam.get("remaining_status"),
            "allow_network": steam.get("allow_network", False),
        }
    )


def classify_collections(
    names: tuple[str, ...], configuration: SteamRuleConfiguration
) -> tuple[CollectionStatus | None, tuple[str, ...], bool]:
    matched = tuple(
        rule for rule in configuration.rules if any(rule.matches(name) for name in names)
    )
    statuses = tuple(dict.fromkeys(rule.status for rule in matched))
    reasons = tuple(
        f"{rule.match}:{rule.pattern}->{rule.status.label}" for rule in matched
    )
    if len(statuses) > 1:
        return None, reasons, True
    if statuses:
        return statuses[0], reasons, False
    return configuration.remaining_status, ("remaining",), False

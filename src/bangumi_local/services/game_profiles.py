from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from bangumi_local.db.models import GameProfile, Work


class GameProfileError(ValueError):
    """Raised for invalid game-profile edits."""


_CONFIDENCE = {"unknown", "low", "medium", "high", "confirmed"}
_COMPLETION = {"unknown", "unplayed", "playing", "completed", "dropped", "paused"}


@dataclass(frozen=True, slots=True)
class GameProfilePatch:
    confidence: str | None = None
    completion: str | None = None
    playtime_minutes: int | None = None
    first_played_at: str | None = None
    last_played_at: str | None = None
    liked_aspects: tuple[str, ...] | None = None
    disliked_aspects: tuple[str, ...] | None = None
    notes_private: str | None = None


@dataclass(frozen=True, slots=True)
class GameProfileResult:
    work_id: int
    changed_fields: tuple[str, ...]


def _clean_aspects(values: tuple[str, ...] | None) -> str | None:
    if values is None:
        return None
    cleaned = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))


def edit_game_profile(
    session: Session, work_id: int, patch: GameProfilePatch
) -> GameProfileResult:
    work = session.get(Work, work_id)
    if work is None:
        raise GameProfileError(f"Work does not exist: {work_id}")
    if work.kind != "game":
        raise GameProfileError("Only game works can have a game profile.")
    if patch.confidence is not None and patch.confidence not in _CONFIDENCE:
        raise GameProfileError(f"Unsupported confidence: {patch.confidence}")
    if patch.completion is not None and patch.completion not in _COMPLETION:
        raise GameProfileError(f"Unsupported completion: {patch.completion}")
    if patch.playtime_minutes is not None and patch.playtime_minutes < 0:
        raise GameProfileError("playtime_minutes cannot be negative.")

    profile = session.get(GameProfile, work_id)
    if profile is None:
        profile = GameProfile(work_id=work_id)
        session.add(profile)
        session.flush()
    values: dict[str, object | None] = {
        "confidence": patch.confidence,
        "completion": patch.completion,
        "playtime_minutes": patch.playtime_minutes,
        "first_played_at": patch.first_played_at,
        "last_played_at": patch.last_played_at,
        "liked_aspects_json": _clean_aspects(patch.liked_aspects),
        "disliked_aspects_json": _clean_aspects(patch.disliked_aspects),
        "notes_private": patch.notes_private,
    }
    changed: list[str] = []
    for field, value in values.items():
        # None means "not supplied" for the first five scalar controls.  The UI
        # uses explicit empty strings/tuples when clearing text/list fields.
        if value is None and field not in {
            "liked_aspects_json",
            "disliked_aspects_json",
            "notes_private",
        }:
            continue
        if getattr(profile, field) != value:
            setattr(profile, field, value)
            changed.append(field)
    return GameProfileResult(work_id=work_id, changed_fields=tuple(changed))

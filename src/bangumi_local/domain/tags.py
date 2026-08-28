from __future__ import annotations

from collections.abc import Iterable

DEFAULT_GALGAME_CLASSIFICATION_TAG = "Galgame分类"
DEFAULT_GAME_CLASSIFICATION_TAG = "普通Game分类"


class TagValidationError(ValueError):
    pass


def validate_tag(value: str) -> str:
    tag = value.strip()
    if not tag:
        raise TagValidationError("Tag must not be empty.")
    if any(ord(character) < 32 or ord(character) == 127 for character in tag):
        raise TagValidationError("Tag must not contain control characters.")
    return tag


def stable_unique(tags: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = validate_tag(raw)
        if tag in seen:
            continue
        seen.add(tag)
        result.append(tag)
    return tuple(result)


def add_tag(tags: Iterable[str], tag: str) -> tuple[str, ...]:
    return stable_unique((*tuple(tags), validate_tag(tag)))


def remove_tag(tags: Iterable[str], tag: str) -> tuple[str, ...]:
    target = validate_tag(tag)
    return stable_unique(value for value in tags if value.strip() != target)


def rename_tag(tags: Iterable[str], old_tag: str, new_tag: str) -> tuple[str, ...]:
    old = validate_tag(old_tag)
    new = validate_tag(new_tag)
    if old == new:
        raise TagValidationError("Old and new tags must be different.")
    return stable_unique(new if value.strip() == old else value for value in tags)


def apply_tag_action(
    tags: Iterable[str],
    operation: str,
    *,
    tag: str | None = None,
    old_tag: str | None = None,
    new_tag: str | None = None,
    set_tags: Iterable[str] | None = None,
) -> tuple[str, ...]:
    if operation == "add" and tag is not None:
        return add_tag(tags, tag)
    if operation == "remove" and tag is not None:
        return remove_tag(tags, tag)
    if operation == "rename" and old_tag is not None and new_tag is not None:
        return rename_tag(tags, old_tag, new_tag)
    if operation == "set" and set_tags is not None:
        return stable_unique(set_tags)
    raise ValueError(f"Invalid tag action: {operation}")

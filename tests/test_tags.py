from __future__ import annotations

import pytest

from bangumi_local.domain.tags import (
    DEFAULT_GALGAME_CLASSIFICATION_TAG,
    DEFAULT_GAME_CLASSIFICATION_TAG,
    TagValidationError,
    add_tag,
    remove_tag,
    rename_tag,
    stable_unique,
)


def test_tag_operations_preserve_order_and_unrelated_values() -> None:
    assert stable_unique(("RPG", "单机", "RPG")) == ("RPG", "单机")
    assert add_tag(("RPG", "单机"), "Galgame") == ("RPG", "单机", "Galgame")
    assert add_tag(("RPG", "Galgame"), "Galgame") == ("RPG", "Galgame")
    assert remove_tag(("RPG", "Galgame", "单机"), "Galgame") == ("RPG", "单机")
    assert rename_tag(("Gal Game", "RPG", "Galgame"), "Gal Game", "Galgame") == (
        "Galgame",
        "RPG",
    )


def test_default_classification_markers_avoid_case_only_collisions() -> None:
    assert DEFAULT_GALGAME_CLASSIFICATION_TAG == "Galgame分类"
    assert DEFAULT_GAME_CLASSIFICATION_TAG == "普通Game分类"
    assert DEFAULT_GALGAME_CLASSIFICATION_TAG.casefold() != "galgame"
    assert DEFAULT_GAME_CLASSIFICATION_TAG.casefold() != "game"


@pytest.mark.parametrize("value", ("", "   ", "bad\ntag", "bad\x00tag"))
def test_invalid_tags_are_rejected(value: str) -> None:
    with pytest.raises(TagValidationError):
        add_tag((), value)


def test_rename_rejects_identical_tags() -> None:
    with pytest.raises(TagValidationError):
        rename_tag(("Galgame",), "Galgame", "Galgame")

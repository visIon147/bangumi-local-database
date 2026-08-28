from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

CANONICAL_FIELDS: tuple[str, ...] = ("type", "rate", "comment", "private", "tags")


def normalized_tags(tags: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({tag.strip() for tag in tags if tag.strip()}))


@dataclass(frozen=True, slots=True)
class CollectionSnapshot:
    collection_type: int | None
    rating: int | None
    comment: str | None
    is_private: bool
    tags: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        collection_type: int | None,
        rating: int | None,
        comment: str | None,
        is_private: bool,
        tags: Iterable[str],
    ) -> CollectionSnapshot:
        return cls(
            collection_type=collection_type,
            rating=None if rating in (None, 0) else rating,
            comment=comment or "",
            is_private=is_private,
            tags=normalized_tags(tags),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CollectionSnapshot:
        return cls.create(
            collection_type=value.get("type"),
            rating=value.get("rate"),
            comment=value.get("comment"),
            is_private=bool(value.get("private", False)),
            tags=value.get("tags", ()),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "comment": self.comment or "",
            "private": self.is_private,
            "rate": self.rating or 0,
            "tags": list(self.tags),
            "type": self.collection_type,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def value_for(self, field: str) -> object:
        if field not in CANONICAL_FIELDS:
            raise KeyError(f"Unknown canonical collection field: {field}")
        return self.as_dict()[field]

    def replacing(self, values: Mapping[str, object]) -> CollectionSnapshot:
        merged = self.as_dict()
        for field, value in values.items():
            if field not in CANONICAL_FIELDS:
                raise KeyError(f"Unknown canonical collection field: {field}")
            merged[field] = value
        return CollectionSnapshot.from_mapping(merged)

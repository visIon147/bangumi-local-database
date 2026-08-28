from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from bangumi_local.domain.models import CollectionStatus
from bangumi_local.domain.snapshots import CANONICAL_FIELDS, CollectionSnapshot


class MutationValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CollectionPatch:
    """A validated, explicit subset of Bangumi collection fields."""

    values: Mapping[str, object]

    def __post_init__(self) -> None:
        values = dict(self.values)
        if not values:
            raise MutationValidationError("Collection patch must contain at least one field.")
        unknown = set(values) - set(CANONICAL_FIELDS)
        if unknown:
            raise MutationValidationError(f"Unsupported collection patch fields: {sorted(unknown)}")
        if "type" in values:
            if isinstance(values["type"], bool):
                raise MutationValidationError("Collection type must be between 1 and 5.")
            try:
                values["type"] = int(CollectionStatus(int(values["type"])))
            except (TypeError, ValueError) as exc:
                raise MutationValidationError("Collection type must be between 1 and 5.") from exc
        if "rate" in values:
            if isinstance(values["rate"], bool):
                raise MutationValidationError("Rating must be between 0 and 10.")
            try:
                rate = int(values["rate"])
            except (TypeError, ValueError) as exc:
                raise MutationValidationError("Rating must be between 0 and 10.") from exc
            if not 0 <= rate <= 10:
                raise MutationValidationError("Rating must be between 0 and 10.")
            values["rate"] = rate
        if "comment" in values and not isinstance(values["comment"], str):
            raise MutationValidationError("Comment must be a string.")
        if "private" in values and not isinstance(values["private"], bool):
            raise MutationValidationError("Private must be a boolean.")
        if "tags" in values:
            raw = values["tags"]
            if isinstance(raw, (str, bytes)):
                raise MutationValidationError("Tags must be a sequence of strings.")
            try:
                tags = tuple(str(item) for item in raw)  # type: ignore[union-attr]
            except TypeError as exc:
                raise MutationValidationError("Tags must be a sequence of strings.") from exc
            for tag in tags:
                if not tag.strip() or any(ord(char) < 32 or ord(char) == 127 for char in tag):
                    raise MutationValidationError("Tags cannot be blank or contain control characters.")
            values["tags"] = tags
        object.__setattr__(self, "values", values)

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(field for field in CANONICAL_FIELDS if field in self.values)

    def as_api_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        for field in self.fields:
            value = self.values[field]
            payload[field] = list(value) if field == "tags" else value
        return payload

    def apply_to(self, snapshot: CollectionSnapshot) -> CollectionSnapshot:
        return snapshot.replacing(self.values)

    @classmethod
    def between(
        cls,
        before: CollectionSnapshot,
        intended: CollectionSnapshot,
        fields: tuple[str, ...],
    ) -> CollectionPatch:
        return cls({field: intended.value_for(field) for field in fields})

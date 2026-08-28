from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from bangumi_local.domain.snapshots import CANONICAL_FIELDS, CollectionSnapshot


class DiffStatus(StrEnum):
    CLEAN = "clean"
    REMOTE_CHANGED = "remote_changed"
    LOCAL_CHANGED = "local_changed"
    CONVERGED = "converged"
    CONFLICT = "conflict"
    BOOTSTRAP_MISSING = "bootstrap_missing"
    REMOTE_MISSING = "remote_missing"


@dataclass(frozen=True, slots=True)
class TagChanges:
    local_add: tuple[str, ...]
    local_remove: tuple[str, ...]
    remote_add: tuple[str, ...]
    remote_remove: tuple[str, ...]

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "local_add": list(self.local_add),
            "local_remove": list(self.local_remove),
            "remote_add": list(self.remote_add),
            "remote_remove": list(self.remote_remove),
        }


@dataclass(frozen=True, slots=True)
class FieldDiff:
    field: str
    status: DiffStatus
    base: object
    local: object
    remote: object
    tag_changes: TagChanges | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "field": self.field,
            "status": self.status.value,
            "base": self.base,
            "local": self.local,
            "remote": self.remote,
        }
        if self.tag_changes is not None:
            result["tag_changes"] = self.tag_changes.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class CollectionDiff:
    status: DiffStatus
    fields: tuple[FieldDiff, ...]
    base: CollectionSnapshot | None
    local: CollectionSnapshot
    remote: CollectionSnapshot | None

    @property
    def changed_fields(self) -> tuple[str, ...]:
        return tuple(field.field for field in self.fields if field.status != DiffStatus.CLEAN)

    def fields_with_status(self, status: DiffStatus) -> tuple[str, ...]:
        return tuple(field.field for field in self.fields if field.status == status)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "changed_fields": list(self.changed_fields),
            "base": self.base.as_dict() if self.base else None,
            "local": self.local.as_dict(),
            "remote": self.remote.as_dict() if self.remote else None,
            "fields": [field.to_dict() for field in self.fields],
        }


def diff_field(field: str, base: object, local: object, remote: object) -> FieldDiff:
    if local == base and remote == base:
        status = DiffStatus.CLEAN
    elif local == base and remote != base:
        status = DiffStatus.REMOTE_CHANGED
    elif local != base and remote == base:
        status = DiffStatus.LOCAL_CHANGED
    elif local == remote:
        status = DiffStatus.CONVERGED
    else:
        status = DiffStatus.CONFLICT

    tag_changes = None
    if field == "tags":
        base_tags = set(base)  # type: ignore[arg-type]
        local_tags = set(local)  # type: ignore[arg-type]
        remote_tags = set(remote)  # type: ignore[arg-type]
        tag_changes = TagChanges(
            local_add=tuple(sorted(local_tags - base_tags)),
            local_remove=tuple(sorted(base_tags - local_tags)),
            remote_add=tuple(sorted(remote_tags - base_tags)),
            remote_remove=tuple(sorted(base_tags - remote_tags)),
        )
    return FieldDiff(
        field=field,
        status=status,
        base=base,
        local=local,
        remote=remote,
        tag_changes=tag_changes,
    )


def diff_collection(
    base: CollectionSnapshot | None,
    local: CollectionSnapshot,
    remote: CollectionSnapshot | None,
) -> CollectionDiff:
    if base is None:
        fields = tuple(
            FieldDiff(
                field=field,
                status=DiffStatus.BOOTSTRAP_MISSING,
                base=None,
                local=local.value_for(field),
                remote=remote.value_for(field) if remote else None,
            )
            for field in CANONICAL_FIELDS
        )
        return CollectionDiff(DiffStatus.BOOTSTRAP_MISSING, fields, base, local, remote)

    if remote is None:
        fields = tuple(
            FieldDiff(
                field=field,
                status=DiffStatus.REMOTE_MISSING,
                base=base.value_for(field),
                local=local.value_for(field),
                remote=None,
            )
            for field in CANONICAL_FIELDS
        )
        return CollectionDiff(DiffStatus.REMOTE_MISSING, fields, base, local, remote)

    fields = tuple(
        diff_field(
            field,
            base.value_for(field),
            local.value_for(field),
            remote.value_for(field),
        )
        for field in CANONICAL_FIELDS
    )
    statuses = {field.status for field in fields}
    if DiffStatus.CONFLICT in statuses:
        summary = DiffStatus.CONFLICT
    elif DiffStatus.LOCAL_CHANGED in statuses:
        summary = DiffStatus.LOCAL_CHANGED
    elif DiffStatus.REMOTE_CHANGED in statuses:
        summary = DiffStatus.REMOTE_CHANGED
    elif DiffStatus.CONVERGED in statuses:
        summary = DiffStatus.CONVERGED
    else:
        summary = DiffStatus.CLEAN
    return CollectionDiff(summary, fields, base, local, remote)


def advance_base_after_pull(
    diff: CollectionDiff,
) -> CollectionSnapshot:
    if diff.base is None or diff.remote is None:
        raise ValueError("Cannot advance a missing base or remote snapshot")
    accepted = {
        field.field: field.remote
        for field in diff.fields
        if field.status in (DiffStatus.REMOTE_CHANGED, DiffStatus.CONVERGED)
    }
    return diff.base.replacing(accepted)


from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from bangumi_local.domain.snapshots import CollectionSnapshot


def stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class PlanCandidate:
    work_id: int | None
    subject_id: int | None
    title: str
    bgm_url: str | None
    disposition: str
    reason: str
    action: dict[str, Any] = field(default_factory=dict)
    selection_evidence: dict[str, Any] = field(default_factory=dict)
    before_snapshot: CollectionSnapshot | None = None
    intended_snapshot: CollectionSnapshot | None = None
    before_tags: tuple[str, ...] = ()
    after_tags: tuple[str, ...] | None = None
    public_tags: tuple[str, ...] = ()
    precondition_hash: str | None = None
    changed_fields: tuple[str, ...] = ()
    local_precondition_hash: str | None = None
    source_entry_id: int | None = None
    source_precondition_hash: str | None = None
    remote_existence: str | None = None

    def immutable_dict(self, *, format_version: int = 1) -> dict[str, object]:
        result: dict[str, object] = {
            "action": self.action,
            "after_tags": list(self.after_tags) if self.after_tags is not None else None,
            "before_snapshot": (
                self.before_snapshot.as_dict()
                if self.before_snapshot is not None
                else (None if format_version >= 3 else {})
            ),
            "before_tags": list(self.before_tags),
            "bgm_url": self.bgm_url,
            "disposition": self.disposition,
            "game_id" if format_version == 1 else "work_id": self.work_id,
            "intended_snapshot": (
                self.intended_snapshot.as_dict() if self.intended_snapshot is not None else None
            ),
            "precondition_hash": self.precondition_hash,
            "public_tags": list(self.public_tags),
            "reason": self.reason,
            "selection_evidence": self.selection_evidence,
            "subject_id": self.subject_id,
            "title": self.title,
        }
        if format_version >= 2:
            result["changed_fields"] = list(self.changed_fields)
            result["local_precondition_hash"] = self.local_precondition_hash
        if format_version >= 3:
            result["source_entry_id"] = self.source_entry_id
            result["source_precondition_hash"] = self.source_precondition_hash
            result["remote_existence"] = self.remote_existence
        return result


def plan_content(
    *,
    kind: str,
    operation: str,
    tag: str | None,
    old_tag: str | None,
    new_tag: str | None,
    selector: dict[str, Any],
    candidates: list[PlanCandidate],
    reverse_of_plan_id: str | None = None,
    format_version: int = 1,
) -> dict[str, object]:
    result: dict[str, object] = {
        "candidates": [
            candidate.immutable_dict(format_version=format_version)
            for candidate in sorted(
                candidates,
                key=lambda item: (
                    item.subject_id is None,
                    item.subject_id or 0,
                    item.source_entry_id or 0,
                ),
            )
        ],
        "kind": kind,
        "new_tag": new_tag,
        "old_tag": old_tag,
        "operation": operation,
        "reverse_of_plan_id": reverse_of_plan_id,
        "selector": selector,
        "tag": tag,
    }
    if format_version >= 2:
        result["format_version"] = format_version
    return result


def content_digest(content: dict[str, object]) -> str:
    return hashlib.sha256(stable_json(content).encode("utf-8")).hexdigest()

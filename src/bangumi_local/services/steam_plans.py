from __future__ import annotations

from dataclasses import replace
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    LibraryCollection,
    LibraryEntry,
    LibraryEntryCollection,
    SourceAccount,
    SyncShadow,
    Work,
)
from bangumi_local.db.repositories import local_snapshot, shadow_snapshot
from bangumi_local.domain.merge import DiffStatus, diff_collection
from bangumi_local.domain.models import CollectionStatus, RemoteCollection
from bangumi_local.domain.plans import PlanCandidate, stable_json
from bangumi_local.domain.snapshots import CollectionSnapshot
from bangumi_local.domain.steam import SteamRuleConfiguration, classify_collections
from bangumi_local.domain.tags import validate_tag
from bangumi_local.services.plans import PlanError, StoredPlan, _save_plan
from bangumi_local.services.pull import snapshot_from_remote
from bangumi_local.services.steam_library import steam_account


def steam_source_precondition(
    session: Session, entry_id: int
) -> tuple[str, tuple[str, ...]]:
    entry = session.get(LibraryEntry, entry_id)
    if entry is None:
        raise PlanError("Steam source entry is missing.")
    names = tuple(
        session.scalars(
            select(LibraryCollection.name)
            .join(
                LibraryEntryCollection,
                LibraryEntryCollection.collection_id == LibraryCollection.id,
            )
            .where(
                LibraryEntryCollection.library_entry_id == entry.id,
                LibraryEntryCollection.active.is_(True),
                LibraryCollection.active.is_(True),
            )
            .order_by(LibraryCollection.name)
        ).all()
    )
    payload = {
        "entry_id": entry.id,
        "external_id": entry.external_id,
        "source_hash": entry.source_hash,
        "match_status": entry.match_status,
        "work_id": entry.work_id,
        "collections": list(names),
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest(), names


def absent_local_precondition() -> str:
    return hashlib.sha256(
        stable_json({"collection_state": None, "shadow": None}).encode("utf-8")
    ).hexdigest()


def _rules_payload(configuration: SteamRuleConfiguration) -> dict[str, object]:
    return {
        "allow_network": configuration.allow_network,
        "remaining_status": (
            configuration.remaining_status.label
            if configuration.remaining_status is not None
            else None
        ),
        "rules": [
            {
                "match": rule.match,
                "pattern": rule.pattern,
                "status": rule.status.label,
                "case_sensitive": rule.case_sensitive,
            }
            for rule in configuration.rules
        ],
    }


def create_steam_status_plan(
    session: Session,
    collections: list[RemoteCollection],
    *,
    app_ids: tuple[str, ...] | None,
    all_eligible: bool,
    account_id: str | None,
    configuration: SteamRuleConfiguration,
    remaining_status: CollectionStatus | None,
    followup_tag: str | None,
    rules_source: str = "saved_defaults",
) -> StoredPlan:
    if (app_ids is None) == (not all_eligible):
        raise PlanError("Choose exactly one of appids or all_eligible.")
    if remaining_status is not None:
        configuration = replace(configuration, remaining_status=remaining_status)
    if followup_tag is not None:
        followup_tag = validate_tag(followup_tag)
    account: SourceAccount = steam_account(session, account_id)
    statement = select(LibraryEntry).where(LibraryEntry.source_account_id == account.id)
    if app_ids is not None:
        statement = statement.where(LibraryEntry.external_id.in_(app_ids))
    entries = session.scalars(statement.order_by(LibraryEntry.external_id)).all()
    if app_ids is not None:
        found = {item.external_id for item in entries}
        missing = sorted(set(app_ids) - found, key=int)
        if missing:
            raise PlanError(f"Steam AppIDs are not imported: {missing}")
    remote_by_id = {item.subject_id: item for item in collections}
    rules_payload = _rules_payload(configuration)
    selector = {
        "mode": "steam_appids" if app_ids is not None else "steam_all_eligible",
        "app_ids": list(app_ids or ()),
        "rules": rules_payload,
        "rules_hash": hashlib.sha256(stable_json(rules_payload).encode("utf-8")).hexdigest(),
        "rules_source": rules_source,
        "followup_tag": followup_tag,
    }
    candidates: list[PlanCandidate] = []
    for entry in entries:
        source_hash, collection_names = steam_source_precondition(session, entry.id)
        desired_status, rule_reasons, rule_conflict = classify_collections(
            collection_names, configuration
        )
        work = session.get(Work, entry.work_id) if entry.work_id is not None else None
        identity = (
            session.scalar(
                select(BangumiSubject).where(BangumiSubject.work_id == work.id)
            )
            if work is not None
            else None
        )
        title = entry.title_observed or f"Steam App {entry.external_id}"
        evidence: dict[str, object] = {
            "steam_app_id": entry.external_id,
            "steam_collections": list(collection_names),
            "rule_reasons": list(rule_reasons),
            "match_status": entry.match_status,
        }
        reason: str | None = None
        if entry.match_status != "confirmed":
            reason = {
                "no_subject": "steam_no_subject",
                "deferred": "steam_match_deferred",
                "candidates": "steam_match_unconfirmed",
            }.get(entry.match_status, "steam_unmatched")
        elif work is None or identity is None or identity.subject_type != 4:
            reason = "steam_mapping_invalid"
        elif rule_conflict:
            reason = "steam_category_rule_conflict"
        elif desired_status is None:
            reason = "steam_remaining_local_only"

        remote = remote_by_id.get(identity.subject_id) if identity is not None else None
        before = snapshot_from_remote(remote) if remote is not None else None
        intended: CollectionSnapshot | None = None
        action: dict[str, object] = {"operation": "none", "values": {}}
        disposition = "unchanged"
        remote_existence = "present" if remote is not None else "absent"
        precondition_hash = before.digest() if before is not None else None
        local_precondition_hash: str | None = None
        changed_fields: tuple[str, ...] = ()

        if reason is None and desired_status is not None and identity is not None:
            evidence["desired_status"] = desired_status.label
            state = session.get(BangumiCollectionState, identity.subject_id)
            shadow = session.get(SyncShadow, identity.subject_id)
            if remote is None:
                if state is not None or shadow is not None:
                    reason = "steam_remote_missing_local_state_exists"
                else:
                    intended = CollectionSnapshot.create(
                        collection_type=int(desired_status),
                        rating=None,
                        comment=None,
                        is_private=False,
                        tags=(),
                    )
                    action = {
                        "operation": "create_collection",
                        "fields": ["type"],
                        "values": {"type": int(desired_status)},
                    }
                    disposition = "planned"
                    reason = "steam_create_collection"
                    changed_fields = ("type",)
                    local_precondition_hash = absent_local_precondition()
            else:
                assert before is not None
                if before.collection_type == int(desired_status):
                    intended = before
                    reason = "steam_status_already_desired"
                elif state is None or shadow is None:
                    reason = "missing_shadow"
                else:
                    local = local_snapshot(session, identity.subject_id)
                    base = shadow_snapshot(shadow)
                    field = next(
                        item for item in diff_collection(base, local, before).fields if item.field == "type"
                    )
                    evidence["type_field_status"] = field.status.value
                    if field.status != DiffStatus.CLEAN:
                        reason = {
                            DiffStatus.LOCAL_CHANGED: "steam_local_changed",
                            DiffStatus.REMOTE_CHANGED: "remote_changed_pull_required",
                            DiffStatus.CONFLICT: "conflict",
                            DiffStatus.CONVERGED: "converged_pull_required",
                        }.get(field.status, "steam_type_not_clean")
                    else:
                        intended = before.replacing({"type": int(desired_status)})
                        action = {
                            "operation": "patch_collection",
                            "fields": ["type"],
                            "values": {"type": int(desired_status)},
                        }
                        disposition = "planned"
                        reason = "steam_patch_collection"
                        changed_fields = ("type",)
                        local_precondition_hash = local.digest()

        candidates.append(
            PlanCandidate(
                work_id=work.id if work is not None else None,
                subject_id=identity.subject_id if identity is not None else None,
                source_entry_id=entry.id,
                title=title,
                bgm_url=identity.url if identity is not None else None,
                disposition=disposition,
                reason=reason or "steam_not_actionable",
                action=action,
                selection_evidence=evidence,
                before_snapshot=before,
                intended_snapshot=intended,
                before_tags=before.tags if before is not None else (),
                after_tags=intended.tags if intended is not None else None,
                public_tags=remote.subject.public_tags if remote is not None else (),
                precondition_hash=precondition_hash,
                changed_fields=changed_fields,
                local_precondition_hash=local_precondition_hash,
                source_precondition_hash=source_hash,
                remote_existence=remote_existence,
            )
        )
    return _save_plan(
        session,
        kind="steam_status",
        operation="collection_sync",
        selector=selector,
        candidates=candidates,
        format_version=3,
    )

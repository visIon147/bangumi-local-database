from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    DiscoveryCandidate,
    DiscoveryReviewState,
    GameProfile,
    SyncShadow,
    Work,
    WorkLink,
)
from bangumi_local.db.repositories import local_snapshot, shadow_snapshot, utc_now_iso
from bangumi_local.domain.merge import DiffStatus, diff_collection
from bangumi_local.domain.models import (
    CollectionStatus,
    RemoteCollection,
    SubjectSearchCandidate,
    SubjectType,
)
from bangumi_local.domain.plans import PlanCandidate
from bangumi_local.domain.snapshots import CollectionSnapshot
from bangumi_local.services.discovery import DiscoveryError
from bangumi_local.services.plans import StoredPlan, _save_plan
from bangumi_local.services.pull import snapshot_from_remote
from bangumi_local.services.steam_plans import absent_local_precondition


@dataclass(frozen=True, slots=True)
class IdentityPromotionResult:
    candidate_id: str
    work_id: int
    subject_id: int
    created: bool


def promote_bangumi_identity(
    session: Session,
    candidate_id: str,
    *,
    verified_subject: SubjectSearchCandidate,
) -> IdentityPromotionResult:
    """Persist an explicitly verified Bangumi identity without creating a collection."""

    candidate = session.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        raise DiscoveryError(f"Discovery candidate not found: {candidate_id}")
    if candidate.item_status == "identity_conflict":
        raise DiscoveryError("Identity conflict must be resolved before promotion.")
    if candidate.subject_id is None:
        raise DiscoveryError("Candidate has no Bangumi identity to promote.")
    if verified_subject.subject_id != candidate.subject_id:
        raise DiscoveryError("Fresh subject verification does not match the candidate.")
    if verified_subject.subject_type != SubjectType.GAME:
        raise DiscoveryError("Discovery promotion only accepts Bangumi game subjects.")

    identity = session.get(BangumiSubject, candidate.subject_id)
    created = identity is None
    if identity is None:
        now = utc_now_iso()
        work = Work(
            kind="game",
            title=verified_subject.display_title,
            title_cn=verified_subject.title_cn,
            title_original=verified_subject.title_original,
            summary=verified_subject.summary,
            release_date=verified_subject.release_date,
            cover_url=verified_subject.cover_url,
            created_at=now,
            updated_at=now,
            bgm_subject_id=verified_subject.subject_id,
            bgm_url=verified_subject.url,
        )
        session.add(work)
        session.flush()
        identity = BangumiSubject(
            subject_id=verified_subject.subject_id,
            work_id=work.id,
            subject_type=int(SubjectType.GAME),
            url=verified_subject.url,
            metadata_available=True,
            last_observed_at=now,
        )
        session.add(identity)
        session.add(GameProfile(work_id=work.id))
        session.add(
            WorkLink(
                work_id=work.id,
                source="bangumi",
                url=verified_subject.url,
                external_id=str(verified_subject.subject_id),
                is_primary=True,
                match_source="discovery_promotion",
                match_confidence="confirmed",
                verified_at=now,
            )
        )
    else:
        work = session.get(Work, identity.work_id)
        if work is None:
            raise DiscoveryError("Bangumi identity references a missing work.")

    candidate.work_id = work.id
    review = (
        session.get(DiscoveryReviewState, candidate.review_state_id)
        if candidate.review_state_id is not None
        else None
    )
    if review is not None:
        if review.work_id not in (None, work.id) or review.subject_id not in (
            None,
            candidate.subject_id,
        ):
            raise DiscoveryError("Review state identity conflicts with the promoted work.")
        review.work_id = work.id
        review.subject_id = candidate.subject_id
        review.updated_at = utc_now_iso()
    return IdentityPromotionResult(
        candidate_id=candidate.id,
        work_id=work.id,
        subject_id=candidate.subject_id,
        created=created,
    )


def create_discovery_status_plan(
    session: Session,
    candidate_id: str,
    *,
    status: CollectionStatus,
    remote: RemoteCollection | None,
) -> StoredPlan:
    """Create one immutable status draft; never infers status from the decision."""

    candidate = session.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        raise DiscoveryError(f"Discovery candidate not found: {candidate_id}")
    if candidate.work_id is None or candidate.subject_id is None:
        raise DiscoveryError("Confirm or promote an identity before creating a status plan.")
    work = session.get(Work, candidate.work_id)
    identity = session.get(BangumiSubject, candidate.subject_id)
    if work is None or identity is None or identity.work_id != work.id:
        raise DiscoveryError("Candidate identity is incomplete or inconsistent.")
    if remote is not None and remote.subject_id != candidate.subject_id:
        raise DiscoveryError("Fresh remote collection does not match the candidate.")

    before = snapshot_from_remote(remote) if remote is not None else None
    state = session.get(BangumiCollectionState, candidate.subject_id)
    shadow = session.get(SyncShadow, candidate.subject_id)
    disposition = "unchanged"
    reason = "discovery_status_not_actionable"
    action: dict[str, object] = {"operation": "none", "values": {}}
    intended: CollectionSnapshot | None = None
    changed_fields: tuple[str, ...] = ()
    local_hash: str | None = None

    if before is None:
        if state is None and shadow is None:
            intended = CollectionSnapshot.create(
                collection_type=int(status),
                rating=None,
                comment=None,
                is_private=False,
                tags=(),
            )
            action = {
                "operation": "create_collection",
                "fields": ["type"],
                "values": {"type": int(status)},
            }
            disposition = "planned"
            reason = "discovery_create_collection"
            changed_fields = ("type",)
            local_hash = absent_local_precondition()
        else:
            reason = "remote_missing_local_state_exists"
    elif before.collection_type == int(status):
        intended = before
        reason = "discovery_status_already_desired"
    elif state is None or shadow is None:
        reason = "missing_shadow"
    else:
        local = local_snapshot(session, candidate.subject_id)
        base = shadow_snapshot(shadow)
        field = next(
            item
            for item in diff_collection(base, local, before).fields
            if item.field == "type"
        )
        if field.status == DiffStatus.CLEAN:
            intended = before.replacing({"type": int(status)})
            action = {
                "operation": "patch_collection",
                "fields": ["type"],
                "values": {"type": int(status)},
            }
            disposition = "planned"
            reason = "discovery_patch_collection"
            changed_fields = ("type",)
            local_hash = local.digest()
        else:
            reason = {
                DiffStatus.LOCAL_CHANGED: "local_changed",
                DiffStatus.REMOTE_CHANGED: "remote_changed_pull_required",
                DiffStatus.CONFLICT: "conflict",
                DiffStatus.CONVERGED: "converged_pull_required",
            }.get(field.status, "status_not_clean")

    plan_candidate = PlanCandidate(
        work_id=work.id,
        subject_id=identity.subject_id,
        source_entry_id=None,
        title=work.title,
        bgm_url=identity.url,
        disposition=disposition,
        reason=reason,
        action=action,
        selection_evidence={
            "discovery_candidate_id": candidate.id,
            "candidate_key": candidate.candidate_key,
            "decision": candidate.decision,
            "explicit_status": status.label,
        },
        before_snapshot=before,
        intended_snapshot=intended,
        before_tags=before.tags if before else (),
        after_tags=intended.tags if intended else None,
        public_tags=remote.subject.public_tags if remote else (),
        precondition_hash=before.digest() if before else None,
        changed_fields=changed_fields,
        local_precondition_hash=local_hash,
        source_precondition_hash=candidate.snapshot_hash,
        remote_existence="present" if before else "absent",
    )
    return _save_plan(
        session,
        kind="discovery_status",
        operation="collection_sync",
        selector={
            "mode": "discovery_candidate",
            "candidate_id": candidate.id,
            "candidate_snapshot_hash": candidate.snapshot_hash,
            "status": status.label,
        },
        candidates=[plan_candidate],
        format_version=3,
    )

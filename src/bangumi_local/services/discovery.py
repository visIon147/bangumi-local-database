from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    DiscoveryCandidate,
    DiscoveryReviewEvent,
    DiscoveryReviewState,
    DiscoverySession,
    LibraryCollection,
    LibraryEntry,
    LibraryEntryCollection,
    MediaBinding,
    SourceAccount,
    Work,
)
from bangumi_local.db.repositories import utc_now_iso
from bangumi_local.domain.discovery import DiscoveryPromotionPreview
from bangumi_local.domain.models import SubjectSearchCandidate
from bangumi_local.domain.plans import stable_json
from bangumi_local.services.steam_library import steam_account


class DiscoveryError(ValueError):
    pass


class DiscoveryIdentityConflict(DiscoveryError):
    """A candidate was persistently marked as requiring identity review."""


@dataclass(frozen=True, slots=True)
class DeletedDiscoverySession:
    session_id: str
    candidate_count: int


DISCOVERY_DECISIONS = {"played", "not_played", "unsure", "deferred"}


@dataclass(frozen=True, slots=True)
class DiscoverySeed:
    candidate_key: str
    work_id: int | None
    subject_id: int | None
    library_entry_id: int | None
    title: str
    cover_url: str | None
    summary: str | None
    public_tags: tuple[str, ...]
    evidence: dict[str, object]
    priority_score: int
    item_status: str = "pending"


@dataclass(frozen=True, slots=True)
class DiscoverySessionView:
    session: DiscoverySession
    candidates: tuple[DiscoveryCandidate, ...]


def _digest(value: object) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _review_matches(session: Session, seed: DiscoverySeed) -> list[DiscoveryReviewState]:
    conditions = [DiscoveryReviewState.candidate_key == seed.candidate_key]
    if seed.work_id is not None:
        conditions.append(DiscoveryReviewState.work_id == seed.work_id)
    if seed.subject_id is not None:
        conditions.append(DiscoveryReviewState.subject_id == seed.subject_id)
    if seed.library_entry_id is not None:
        conditions.append(DiscoveryReviewState.library_entry_id == seed.library_entry_id)
    return list(session.scalars(select(DiscoveryReviewState).where(or_(*conditions))).all())


def _identity_for_work(session: Session, work_id: int | None) -> tuple[int | None, bool]:
    if work_id is None:
        return None, False
    identity = session.scalar(
        select(BangumiSubject).where(BangumiSubject.work_id == work_id)
    )
    if identity is None:
        return None, False
    collection_exists = session.get(BangumiCollectionState, identity.subject_id) is not None
    return identity.subject_id, collection_exists


def steam_discovery_seeds(
    session: Session,
    *,
    account_id: str | None,
    include_owned_unplayed: bool,
    include_decided: bool,
    max_items: int,
) -> tuple[DiscoverySeed, ...]:
    if not 1 <= max_items <= 200:
        raise DiscoveryError("Discovery max_items must be between 1 and 200.")
    account = steam_account(session, account_id)
    entries = session.scalars(
        select(LibraryEntry)
        .where(LibraryEntry.source_account_id == account.id)
        .order_by(LibraryEntry.external_id)
    ).all()
    memberships = session.execute(
        select(LibraryEntryCollection.library_entry_id, LibraryCollection.name)
        .join(LibraryCollection, LibraryCollection.id == LibraryEntryCollection.collection_id)
        .where(
            LibraryCollection.source_account_id == account.id,
            LibraryCollection.active.is_(True),
            LibraryEntryCollection.active.is_(True),
        )
    ).all()
    collections: dict[int, list[str]] = {}
    for entry_id, name in memberships:
        collections.setdefault(entry_id, []).append(name)
    seeds: list[DiscoverySeed] = []
    for entry in entries:
        subject_id, collection_exists = _identity_for_work(session, entry.work_id)
        if collection_exists:
            continue
        names = tuple(sorted(collections.get(entry.id, [])))
        strong_collection = any("完结" in name or name == "在用" for name in names)
        has_playtime = bool(entry.playtime_minutes and entry.playtime_minutes > 0)
        has_install_evidence = bool(entry.installed or entry.last_played_at)
        owned_only = entry.ownership_scope in {"owned", "visible"}
        if not (strong_collection or has_playtime or has_install_evidence):
            if not (include_owned_unplayed and owned_only):
                continue
        priority = 100 if strong_collection else 0
        if entry.playtime_minutes and entry.playtime_minutes > 600:
            priority += 80
        elif has_playtime:
            priority += 60
        if has_install_evidence:
            priority += 40
        if owned_only:
            priority += 20
        key = (
            f"bangumi:{subject_id}"
            if subject_id is not None
            else f"steam:{account.external_account_id}:{entry.external_id}"
        )
        seed = DiscoverySeed(
            candidate_key=key,
            work_id=entry.work_id,
            subject_id=subject_id,
            library_entry_id=entry.id,
            title=entry.title_observed or f"Steam AppID {entry.external_id}",
            cover_url=None,
            summary=None,
            public_tags=(),
            evidence={
                "provider": "steam",
                "steam_app_id": entry.external_id,
                "collections": list(names),
                "playtime_minutes": entry.playtime_minutes,
                "installed": entry.installed,
                "last_played_at": entry.last_played_at,
                "ownership_scope": entry.ownership_scope,
            },
            priority_score=priority,
        )
        matches = _review_matches(session, seed)
        decisions = {item.decision for item in matches if item.decision is not None}
        if len(decisions) > 1:
            seed = DiscoverySeed(
                candidate_key=seed.candidate_key,
                work_id=seed.work_id,
                subject_id=seed.subject_id,
                library_entry_id=seed.library_entry_id,
                title=seed.title,
                cover_url=seed.cover_url,
                summary=seed.summary,
                public_tags=seed.public_tags,
                evidence=seed.evidence,
                priority_score=seed.priority_score,
                item_status="identity_conflict",
            )
        elif decisions and not include_decided:
            continue
        seeds.append(seed)
    seeds.sort(key=lambda item: (-item.priority_score, item.candidate_key))
    return tuple(seeds[:max_items])


def bangumi_discovery_seeds(
    session: Session,
    candidates: list[SubjectSearchCandidate],
    *,
    provider: str,
    include_decided: bool,
) -> tuple[DiscoverySeed, ...]:
    seeds: list[DiscoverySeed] = []
    seen: set[int] = set()
    for rank, candidate in enumerate(candidates, 1):
        if candidate.subject_id in seen:
            continue
        seen.add(candidate.subject_id)
        identity = session.get(BangumiSubject, candidate.subject_id)
        work_id = identity.work_id if identity is not None else None
        if session.get(BangumiCollectionState, candidate.subject_id) is not None:
            continue
        seed = DiscoverySeed(
            candidate_key=f"bangumi:{candidate.subject_id}",
            work_id=work_id,
            subject_id=candidate.subject_id,
            library_entry_id=None,
            title=candidate.display_title,
            cover_url=candidate.cover_url,
            summary=candidate.summary,
            public_tags=candidate.public_tags,
            evidence={
                "provider": provider,
                "result_rank": rank,
                "release_date": candidate.release_date,
                "title_original": candidate.title_original,
                "aliases": list(candidate.aliases),
                "rank": candidate.rank,
                "score": candidate.score,
                "rating_count": candidate.rating_count,
            },
            priority_score=max(1, 1000 - rank),
        )
        matches = _review_matches(session, seed)
        decisions = {item.decision for item in matches if item.decision is not None}
        if len(decisions) > 1:
            seed = DiscoverySeed(
                candidate_key=seed.candidate_key,
                work_id=seed.work_id,
                subject_id=seed.subject_id,
                library_entry_id=seed.library_entry_id,
                title=seed.title,
                cover_url=seed.cover_url,
                summary=seed.summary,
                public_tags=seed.public_tags,
                evidence=seed.evidence,
                priority_score=seed.priority_score,
                item_status="identity_conflict",
            )
        elif decisions and not include_decided:
            continue
        seeds.append(seed)
    return tuple(seeds)


def create_discovery_session(
    session: Session,
    *,
    provider: str,
    filters: dict[str, object],
    seeds: tuple[DiscoverySeed, ...],
) -> DiscoverySessionView:
    if provider not in {"steam", "bangumi_search", "bangumi_browse"}:
        raise DiscoveryError(f"Unsupported discovery provider: {provider}")
    now = utc_now_iso()
    session_id = str(uuid4())
    row = DiscoverySession(
        id=session_id,
        provider=provider,
        filters_json=stable_json(filters),
        config_hash=_digest({"provider": provider, "filters": filters}),
        cursor_position=0,
        item_count=len(seeds),
        status="active" if seeds else "completed",
        created_at=now,
        updated_at=now,
        error=None,
    )
    session.add(row)
    session.flush()
    candidate_rows: list[DiscoveryCandidate] = []
    for position, seed in enumerate(seeds):
        snapshot = {
            "candidate_key": seed.candidate_key,
            "work_id": seed.work_id,
            "subject_id": seed.subject_id,
            "library_entry_id": seed.library_entry_id,
            "title": seed.title,
            "cover_url": seed.cover_url,
            "summary": seed.summary,
            "public_tags": list(seed.public_tags),
            "evidence": seed.evidence,
            "priority_score": seed.priority_score,
        }
        candidate = DiscoveryCandidate(
            id=str(uuid4()), session_id=session_id, review_state_id=None,
            candidate_key=seed.candidate_key, position=position,
            work_id=seed.work_id, subject_id=seed.subject_id,
            library_entry_id=seed.library_entry_id, title=seed.title,
            cover_url=seed.cover_url, summary=seed.summary,
            public_tags_json=stable_json(list(seed.public_tags)),
            evidence_json=stable_json(seed.evidence),
            priority_score=seed.priority_score, snapshot_hash=_digest(snapshot),
            item_status=seed.item_status, decision=None,
            decision_reason=None, decided_at=None,
        )
        session.add(candidate)
        candidate_rows.append(candidate)
    session.flush()
    return DiscoverySessionView(row, tuple(candidate_rows))


def create_failed_discovery_session(
    session: Session, *, provider: str, filters: dict[str, object], error: str
) -> DiscoverySession:
    now = utc_now_iso()
    row = DiscoverySession(
        id=str(uuid4()), provider=provider, filters_json=stable_json(filters),
        config_hash=_digest({"provider": provider, "filters": filters}),
        cursor_position=0, item_count=0, status="failed",
        created_at=now, updated_at=now, error=error,
    )
    session.add(row)
    session.flush()
    return row


def load_discovery_session(session: Session, session_id: str) -> DiscoverySessionView:
    row = session.get(DiscoverySession, session_id)
    if row is None:
        raise DiscoveryError(f"Discovery session not found: {session_id}")
    candidates = session.scalars(
        select(DiscoveryCandidate)
        .where(DiscoveryCandidate.session_id == session_id)
        .order_by(DiscoveryCandidate.position)
    ).all()
    return DiscoverySessionView(row, tuple(candidates))


def list_discovery_sessions(session: Session) -> tuple[DiscoverySession, ...]:
    return tuple(
        session.scalars(select(DiscoverySession).order_by(DiscoverySession.created_at.desc())).all()
    )


def delete_discovery_session(
    session: Session, session_id: str
) -> DeletedDiscoverySession:
    """Delete one queue snapshot while retaining global decisions and audit events."""

    view = load_discovery_session(session, session_id)
    candidate_count = len(view.candidates)
    events = session.scalars(
        select(DiscoveryReviewEvent).where(
            DiscoveryReviewEvent.session_id == session_id
        )
    ).all()
    for event in events:
        event.session_id = None
    for candidate in view.candidates:
        candidate_events = session.scalars(
            select(DiscoveryReviewEvent).where(
                DiscoveryReviewEvent.candidate_id == candidate.id
            )
        ).all()
        for event in candidate_events:
            event.candidate_id = None
        bindings = session.scalars(
            select(MediaBinding).where(
                MediaBinding.discovery_candidate_id == candidate.id
            )
        ).all()
        for binding in bindings:
            session.delete(binding)
        session.delete(candidate)
    session.flush()
    session.delete(view.session)
    session.flush()
    return DeletedDiscoverySession(
        session_id=session_id,
        candidate_count=candidate_count,
    )


def next_discovery_candidate(session: Session, session_id: str) -> DiscoveryCandidate | None:
    view = load_discovery_session(session, session_id)
    pending = [item for item in view.candidates if item.item_status == "pending"]
    if not pending:
        if view.session.status == "active":
            view.session.status = "completed"
            view.session.updated_at = utc_now_iso()
        return None
    after = [item for item in pending if item.position >= view.session.cursor_position]
    return (after or pending)[0]


def decide_discovery_candidate(
    session: Session,
    session_id: str,
    candidate_id: str,
    *,
    decision: str,
    reason: str | None,
) -> DiscoveryCandidate:
    if decision not in DISCOVERY_DECISIONS:
        raise DiscoveryError(f"Decision must be one of {sorted(DISCOVERY_DECISIONS)}")
    view = load_discovery_session(session, session_id)
    candidate = session.get(DiscoveryCandidate, candidate_id)
    if candidate is None or candidate.session_id != session_id:
        raise DiscoveryError("Discovery candidate is not part of the requested session.")
    if candidate.item_status != "pending":
        raise DiscoveryError(f"Discovery candidate is {candidate.item_status}.")
    seed = DiscoverySeed(
        candidate_key=candidate.candidate_key, work_id=candidate.work_id,
        subject_id=candidate.subject_id, library_entry_id=candidate.library_entry_id,
        title=candidate.title, cover_url=candidate.cover_url, summary=candidate.summary,
        public_tags=tuple(json.loads(candidate.public_tags_json)),
        evidence=json.loads(candidate.evidence_json), priority_score=candidate.priority_score,
    )
    matches = _review_matches(session, seed)
    existing_decisions = {item.decision for item in matches if item.decision is not None}
    if len(existing_decisions) > 1:
        candidate.item_status = "identity_conflict"
        raise DiscoveryIdentityConflict(
            "Conflicting historical decisions exist for this identity."
        )
    review = next((item for item in matches if item.candidate_key == candidate.candidate_key), None)
    if review is None:
        review = matches[0] if len(matches) == 1 else DiscoveryReviewState(
            id=str(uuid4()), candidate_key=candidate.candidate_key,
            work_id=candidate.work_id, subject_id=candidate.subject_id,
            library_entry_id=candidate.library_entry_id, decision=None,
            reason_private=None, decided_at=None, updated_at=utc_now_iso(),
        )
        if not matches:
            session.add(review)
            session.flush()
    previous = review.decision
    now = utc_now_iso()
    review.decision = decision
    review.reason_private = reason
    review.decided_at = now
    review.updated_at = now
    candidate.review_state_id = review.id
    candidate.item_status = "decided"
    candidate.decision = decision
    candidate.decision_reason = reason
    candidate.decided_at = now
    view.session.cursor_position = max(view.session.cursor_position, candidate.position + 1)
    view.session.updated_at = now
    session.flush()
    remaining = session.scalar(
        select(func.count())
        .select_from(DiscoveryCandidate)
        .where(
            DiscoveryCandidate.session_id == session_id,
            DiscoveryCandidate.item_status == "pending",
        )
    )
    if not remaining:
        view.session.status = "completed"
    session.add(
        DiscoveryReviewEvent(
            id=str(uuid4()), session_id=session_id, candidate_id=candidate.id,
            review_state_id=review.id, action="decide",
            previous_decision=previous, next_decision=decision,
            reason_private=reason, created_at=now,
        )
    )
    return candidate


def reopen_discovery_candidate(
    session: Session, candidate_key: str, *, reason: str | None
) -> DiscoveryReviewState:
    review = session.scalar(
        select(DiscoveryReviewState).where(DiscoveryReviewState.candidate_key == candidate_key)
    )
    if review is None:
        raise DiscoveryError(f"Discovery review state not found: {candidate_key}")
    previous = review.decision
    now = utc_now_iso()
    review.decision = None
    review.reason_private = reason
    review.decided_at = None
    review.updated_at = now
    session.add(
        DiscoveryReviewEvent(
            id=str(uuid4()), session_id=None, candidate_id=None,
            review_state_id=review.id, action="reopen",
            previous_decision=previous, next_decision=None,
            reason_private=reason, created_at=now,
        )
    )
    return review


def promotion_preview(session: Session, candidate_id: str) -> DiscoveryPromotionPreview:
    candidate = session.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        raise DiscoveryError(f"Discovery candidate not found: {candidate_id}")
    if candidate.item_status == "identity_conflict":
        status, detail = "identity_conflict", "Conflicting historical identity decisions require review."
    elif candidate.subject_id is not None and session.get(BangumiCollectionState, candidate.subject_id):
        status, detail = "already_collected", "A local Bangumi collection state already exists."
    elif candidate.work_id is not None and candidate.subject_id is not None:
        status, detail = "existing_identity", "The candidate already resolves to a local work."
    elif candidate.subject_id is not None:
        status, detail = "bangumi_identity_available", "A Bangumi subject can be promoted explicitly later."
    elif candidate.library_entry_id is not None:
        status, detail = "needs_steam_match", "Confirm a Steam-to-Bangumi match before promotion."
    else:
        status, detail = "existing_identity", "The candidate already resolves to a local work."
    return DiscoveryPromotionPreview(
        candidate_id=candidate.id, status=status, work_id=candidate.work_id,
        subject_id=candidate.subject_id, library_entry_id=candidate.library_entry_id,
        detail=detail,
    )

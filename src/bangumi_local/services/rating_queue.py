from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    RatingQueueItem,
    RatingQueueSession,
    RatingReviewEvent,
    RatingReviewState,
    SyncShadow,
    Work,
)
from bangumi_local.db.repositories import apply_remote_fields, local_snapshot, utc_now_iso
from bangumi_local.domain.models import SubjectSearchCandidate
from bangumi_local.domain.plans import stable_json
from bangumi_local.domain.snapshots import CollectionSnapshot


class RatingQueueError(ValueError):
    pass


class RatingQueueStale(RatingQueueError):
    """The queue item was persistently marked stale and must not be retried in-place."""


RATING_ORDERS = {
    "recently-updated",
    "release-date-desc",
    "title",
    "random",
    "subject-type",
    "collection-status",
}


@dataclass(frozen=True, slots=True)
class RatingQueueSeed:
    subject_id: int
    subject_type: int
    title: str
    title_original: str | None
    summary: str | None
    release_date: str | None
    cover_url: str | None
    collection_status: int | None
    remote_updated_at: str | None
    initial_snapshot_json: str
    initial_snapshot_hash: str


@dataclass(frozen=True, slots=True)
class RatingQueueView:
    session: RatingQueueSession
    items: tuple[RatingQueueItem, ...]


def _digest(value: object) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _ensure_review_state(
    session: Session, subject_id: int, rating: int | None
) -> RatingReviewState:
    current = session.get(RatingReviewState, subject_id)
    now = utc_now_iso()
    if current is None:
        current = RatingReviewState(
            subject_id=subject_id,
            state="rated" if rating is not None else "pending",
            last_rating=rating,
            reason_private=None,
            reason_state="unknown",
            source="workflow_bootstrap",
            reviewed_at=now if rating is not None else None,
            updated_at=now,
            version=1,
        )
        session.add(current)
        session.flush()
    return current


def observe_explicit_rating(
    session: Session, subject_id: int, rating: int | None, *, source: str
) -> None:
    if rating is None:
        return
    state = _ensure_review_state(session, subject_id, rating)
    if state.state != "rated" or state.last_rating != rating:
        now = utc_now_iso()
        state.state = "rated"
        state.last_rating = rating
        state.source = source
        state.reviewed_at = now
        state.updated_at = now
        state.version += 1


def prepare_rating_queue(
    session: Session,
    *,
    subject_types: tuple[int, ...],
    collection_statuses: tuple[int, ...],
    include_deferred: bool,
    order_name: str,
    random_seed: int | None,
    max_items: int | None,
) -> tuple[RatingQueueSeed, ...]:
    if order_name not in RATING_ORDERS:
        raise RatingQueueError(f"Unsupported rating queue order: {order_name}")
    if order_name == "random" and random_seed is None:
        raise RatingQueueError("--seed is required when --order random is used.")
    if max_items is not None and max_items < 1:
        raise RatingQueueError("max_items must be positive.")
    statement = (
        select(Work, BangumiSubject, BangumiCollectionState, SyncShadow)
        .join(BangumiSubject, BangumiSubject.work_id == Work.id)
        .join(
            BangumiCollectionState,
            BangumiCollectionState.subject_id == BangumiSubject.subject_id,
        )
        .outerjoin(SyncShadow, SyncShadow.subject_id == BangumiSubject.subject_id)
        .where(
            BangumiSubject.subject_type.in_(subject_types),
            BangumiCollectionState.bgm_collection_type.in_(collection_statuses),
        )
    )
    rows = session.execute(statement).all()
    seeds: list[RatingQueueSeed] = []
    for work, identity, collection, shadow in rows:
        review = session.get(RatingReviewState, identity.subject_id)
        if review is None and collection.rating is not None:
            _ensure_review_state(session, identity.subject_id, collection.rating)
            continue
        if review is not None and review.state in {"rated", "skipped"}:
            continue
        if review is not None and review.state == "deferred" and not include_deferred:
            continue
        snapshot = local_snapshot(session, identity.subject_id)
        seeds.append(
            RatingQueueSeed(
                subject_id=identity.subject_id,
                subject_type=identity.subject_type,
                title=work.title,
                title_original=work.title_original,
                summary=work.summary,
                release_date=work.release_date,
                cover_url=work.cover_url,
                collection_status=collection.bgm_collection_type,
                remote_updated_at=shadow.remote_updated_at if shadow else None,
                initial_snapshot_json=snapshot.to_json(),
                initial_snapshot_hash=snapshot.digest(),
            )
        )

    if order_name == "recently-updated":
        seeds.sort(key=lambda x: (x.remote_updated_at or "", x.subject_id), reverse=True)
    elif order_name == "release-date-desc":
        seeds.sort(key=lambda x: (x.release_date or "", x.subject_id), reverse=True)
    elif order_name == "title":
        seeds.sort(key=lambda x: (x.title.casefold(), x.subject_id))
    elif order_name == "subject-type":
        seeds.sort(key=lambda x: (x.subject_type, x.title.casefold(), x.subject_id))
    elif order_name == "collection-status":
        seeds.sort(key=lambda x: (x.collection_status or 0, x.title.casefold(), x.subject_id))
    else:
        random.Random(random_seed).shuffle(seeds)
    if max_items is not None:
        seeds = seeds[:max_items]
    for seed in seeds:
        _ensure_review_state(session, seed.subject_id, None)
    return tuple(seeds)


def create_rating_queue(
    session: Session,
    seeds: tuple[RatingQueueSeed, ...],
    *,
    selector: dict[str, object],
    order_name: str,
    random_seed: int | None,
    enrichments: dict[int, SubjectSearchCandidate | None] | None = None,
    enrichment_errors: dict[int, str] | None = None,
) -> RatingQueueView:
    now = utc_now_iso()
    session_id = str(uuid4())
    config = {"selector": selector, "order": order_name, "seed": random_seed}
    queue = RatingQueueSession(
        id=session_id,
        selector_json=stable_json(selector),
        config_hash=_digest(config),
        order_name=order_name,
        random_seed=random_seed,
        cursor_position=0,
        item_count=len(seeds),
        status="active" if seeds else "completed",
        created_at=now,
        updated_at=now,
        error=None,
    )
    session.add(queue)
    session.flush()
    items: list[RatingQueueItem] = []
    enrichments = enrichments or {}
    enrichment_errors = enrichment_errors or {}
    for position, seed in enumerate(seeds):
        detail = enrichments.get(seed.subject_id)
        subject_snapshot: dict[str, object] = {
            "subject_id": seed.subject_id,
            "subject_type": seed.subject_type,
            "title": seed.title,
            "title_original": seed.title_original,
            "summary": seed.summary,
            "release_date": seed.release_date,
            "cover_url": seed.cover_url,
            "public_tags": [],
            "rank": None,
            "score": None,
            "rating_count": None,
            "bgm_url": f"https://bgm.tv/subject/{seed.subject_id}",
        }
        enrichment_status = "local"
        error = enrichment_errors.get(seed.subject_id)
        if detail is not None:
            subject_snapshot.update(
                title=detail.display_title,
                title_original=detail.title_original,
                summary=detail.summary,
                release_date=detail.release_date,
                cover_url=detail.cover_url,
                public_tags=list(detail.public_tags),
                rank=detail.rank,
                score=detail.score,
                rating_count=detail.rating_count,
            )
            enrichment_status = "fresh"
        elif error is not None:
            enrichment_status = "failed"
        item = RatingQueueItem(
            id=str(uuid4()),
            session_id=session_id,
            subject_id=seed.subject_id,
            position=position,
            initial_snapshot_json=seed.initial_snapshot_json,
            initial_snapshot_hash=seed.initial_snapshot_hash,
            subject_snapshot_json=stable_json(subject_snapshot),
            enrichment_status=enrichment_status,
            item_status="pending",
            outcome=None,
            decided_at=None,
            error=error,
        )
        session.add(item)
        items.append(item)
    session.flush()
    return RatingQueueView(queue, tuple(items))


def load_rating_queue(session: Session, session_id: str) -> RatingQueueView:
    queue = session.get(RatingQueueSession, session_id)
    if queue is None:
        raise RatingQueueError(f"Rating queue session not found: {session_id}")
    items = session.scalars(
        select(RatingQueueItem)
        .where(RatingQueueItem.session_id == session_id)
        .order_by(RatingQueueItem.position)
    ).all()
    return RatingQueueView(queue, tuple(items))


def list_rating_queues(session: Session) -> tuple[RatingQueueSession, ...]:
    return tuple(
        session.scalars(
            select(RatingQueueSession).order_by(RatingQueueSession.created_at.desc())
        ).all()
    )


def next_rating_item(session: Session, session_id: str) -> RatingQueueItem | None:
    view = load_rating_queue(session, session_id)
    pending = [item for item in view.items if item.item_status == "pending"]
    if not pending:
        if view.session.status == "active":
            view.session.status = "completed"
            view.session.updated_at = utc_now_iso()
        return None
    after = [item for item in pending if item.position >= view.session.cursor_position]
    return (after or pending)[0]


def _queue_item(
    session: Session, session_id: str, subject_id: int
) -> tuple[RatingQueueSession, RatingQueueItem]:
    queue = session.get(RatingQueueSession, session_id)
    if queue is None or queue.status not in {"active", "completed"}:
        raise RatingQueueError("Rating queue session is not active.")
    item = session.scalar(
        select(RatingQueueItem).where(
            RatingQueueItem.session_id == session_id,
            RatingQueueItem.subject_id == subject_id,
        )
    )
    if item is None:
        raise RatingQueueError(f"Subject {subject_id} is not in rating queue {session_id}.")
    if item.item_status != "pending":
        raise RatingQueueError(f"Rating queue item is already {item.item_status}.")
    return queue, item


def _finish_item(
    session: Session, queue: RatingQueueSession, item: RatingQueueItem, outcome: str
) -> None:
    now = utc_now_iso()
    item.item_status = "completed"
    item.outcome = outcome
    item.decided_at = now
    queue.cursor_position = max(queue.cursor_position, item.position + 1)
    queue.updated_at = now
    session.flush()
    remaining = session.scalar(
        select(func.count())
        .select_from(RatingQueueItem)
        .where(
            RatingQueueItem.session_id == queue.id,
            RatingQueueItem.item_status == "pending",
        )
    )
    if not remaining:
        queue.status = "completed"


def _mark_stale_if_local_changed(
    session: Session, item: RatingQueueItem, subject_id: int
) -> CollectionSnapshot:
    initial = json.loads(item.initial_snapshot_json)
    before = local_snapshot(session, subject_id)
    if (
        before.rating != (initial.get("rate") or None)
        or (before.comment or "") != str(initial.get("comment") or "")
    ):
        item.item_status = "stale"
        item.error = "local_rating_or_comment_changed_since_queue_creation"
        raise RatingQueueStale(
            "LOCAL rating/comment changed since queue creation; item marked stale."
        )
    return before


def rate_rating_item(
    session: Session,
    session_id: str,
    subject_id: int,
    *,
    score: int,
    reason: str | None,
    skip_reason: bool,
    publish_reason: bool,
    public_comment: str | None,
    replace_existing_comment: bool,
) -> RatingQueueItem:
    if not 1 <= score <= 10:
        raise RatingQueueError("score must be between 1 and 10.")
    if reason is not None and skip_reason:
        raise RatingQueueError("Choose --reason or --skip-reason, not both.")
    if publish_reason and public_comment is not None:
        raise RatingQueueError("Choose --publish-reason or --public-comment, not both.")
    if publish_reason and not reason:
        raise RatingQueueError("--publish-reason requires --reason.")
    if replace_existing_comment and public_comment is None:
        raise RatingQueueError(
            "--replace-existing-comment requires the full intended --public-comment."
        )
    queue, item = _queue_item(session, session_id, subject_id)
    before = _mark_stale_if_local_changed(session, item, subject_id)

    target_comment: str | None = None
    comment_action = "keep"
    if publish_reason:
        if before.comment:
            comment_action = "keep_existing"
        else:
            target_comment = reason or ""
            comment_action = "publish_reason"
    elif public_comment is not None:
        target_comment = public_comment
        comment_action = "public_comment"
    if target_comment is not None:
        if before.comment and before.comment != target_comment and not replace_existing_comment:
            raise RatingQueueError(
                "Existing comment is non-empty; use --replace-existing-comment with the exact intended text."
            )

    values: dict[str, object] = {"rate": score}
    fields = ["rate"]
    if target_comment is not None:
        values["comment"] = target_comment
        fields.append("comment")
    after = before.replacing(values)
    apply_remote_fields(session, subject_id, after, tuple(fields))
    state = _ensure_review_state(session, subject_id, before.rating)
    previous = state.state
    now = utc_now_iso()
    state.state = "rated"
    state.last_rating = score
    state.reason_private = reason
    state.reason_state = "provided" if reason is not None else "skipped" if skip_reason else "unknown"
    state.source = "rating_queue"
    state.reviewed_at = now
    state.updated_at = now
    state.version += 1
    _finish_item(session, queue, item, "rated")
    session.add(
        RatingReviewEvent(
            id=str(uuid4()), session_id=session_id, queue_item_id=item.id,
            subject_id=subject_id, action="rate", previous_state=previous,
            next_state="rated", rating=score, reason_private=reason,
            reason_state=state.reason_state, comment_action=comment_action,
            before_hash=before.digest(), after_hash=after.digest(), created_at=now,
        )
    )
    return item


def set_rating_disposition(
    session: Session, session_id: str, subject_id: int, *, decision: str, reason: str | None
) -> RatingQueueItem:
    if decision not in {"skipped", "deferred"}:
        raise RatingQueueError("Rating decision must be skipped or deferred.")
    queue, item = _queue_item(session, session_id, subject_id)
    before = _mark_stale_if_local_changed(session, item, subject_id)
    state = _ensure_review_state(session, subject_id, before.rating)
    previous = state.state
    now = utc_now_iso()
    state.state = decision
    state.reason_private = reason
    state.reason_state = "provided" if reason is not None else "unknown"
    state.source = "rating_queue"
    state.reviewed_at = now
    state.updated_at = now
    state.version += 1
    _finish_item(session, queue, item, decision)
    session.add(
        RatingReviewEvent(
            id=str(uuid4()), session_id=session_id, queue_item_id=item.id,
            subject_id=subject_id, action=decision, previous_state=previous,
            next_state=decision, rating=before.rating, reason_private=reason,
            reason_state=state.reason_state, comment_action="keep",
            before_hash=before.digest(), after_hash=before.digest(), created_at=now,
        )
    )
    return item


def reopen_rating_subject(session: Session, subject_id: int, reason: str | None) -> RatingReviewState:
    state = session.get(RatingReviewState, subject_id)
    if state is None:
        raise RatingQueueError(f"Rating review state not found for subject {subject_id}.")
    before = local_snapshot(session, subject_id)
    previous = state.state
    now = utc_now_iso()
    state.state = "pending"
    state.source = "rating_reopen"
    state.reviewed_at = None
    state.updated_at = now
    state.version += 1
    session.add(
        RatingReviewEvent(
            id=str(uuid4()), session_id=None, queue_item_id=None,
            subject_id=subject_id, action="reopen", previous_state=previous,
            next_state="pending", rating=before.rating, reason_private=reason,
            reason_state=state.reason_state, comment_action="keep",
            before_hash=before.digest(), after_hash=before.digest(), created_at=now,
        )
    )
    return state


def rated_subject_ids(session: Session, session_id: str) -> tuple[int, ...]:
    load_rating_queue(session, session_id)
    return tuple(
        session.scalars(
            select(RatingQueueItem.subject_id)
            .where(
                RatingQueueItem.session_id == session_id,
                RatingQueueItem.outcome == "rated",
            )
            .order_by(RatingQueueItem.position)
        ).all()
    )


def rating_queue_counts(session: Session, session_id: str) -> dict[str, int]:
    rows = session.execute(
        select(RatingQueueItem.item_status, RatingQueueItem.outcome, func.count())
        .where(RatingQueueItem.session_id == session_id)
        .group_by(RatingQueueItem.item_status, RatingQueueItem.outcome)
    ).all()
    result = {"total": 0, "pending": 0, "rated": 0, "skipped": 0, "deferred": 0, "stale": 0}
    for status, outcome, count in rows:
        result["total"] += count
        if status in result:
            result[status] += count
        if outcome in result:
            result[outcome] += count
    return result

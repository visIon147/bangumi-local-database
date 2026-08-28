from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from bangumi_local.domain.models import SubjectType

from bangumi_local.services.rating_queue import (
    RATING_ORDERS,
    RatingQueueError,
    RatingQueueStale,
    create_rating_queue,
    list_rating_queues,
    load_rating_queue,
    next_rating_item,
    prepare_rating_queue,
    rate_rating_item,
    rated_subject_ids,
    rating_queue_counts,
    reopen_rating_subject,
    set_rating_disposition,
)
from bangumi_local.services.jobs import enqueue_job
from bangumi_local.web.dependencies import get_session
from bangumi_local.web.routes.media import local_media_url
from bangumi_local.web.presentation import status_label


router = APIRouter(prefix="/rating")


class RatingQueueCreatePayload(BaseModel):
    subject_types: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 6])
    collection_statuses: list[int] = Field(default_factory=lambda: [2, 3, 4, 5])
    include_deferred: bool = False
    order: str = "recently-updated"
    seed: int | None = None
    max_items: int | None = Field(default=None, ge=1, le=10000)
    allow_network: bool = False


class RatingActionPayload(BaseModel):
    score: int = Field(ge=1, le=10)
    reason: str | None = Field(default=None, max_length=10000)
    skip_reason: bool = False
    publish_reason: bool = False
    public_comment: str | None = Field(default=None, max_length=10000)
    replace_existing_comment: bool = False


class PrivateReasonPayload(BaseModel):
    reason: str | None = Field(default=None, max_length=10000)


class RatingEnrichmentPayload(BaseModel):
    scope: Literal["current", "session"] = "current"
    subject_id: int | None = Field(default=None, ge=1)
    cache_image: bool = True


def _error(exc: RatingQueueError, status: int = 409) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


@router.get("", response_class=HTMLResponse)
def rating_list(
    request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="rating/list.html",
        context={
            "sessions": list_rating_queues(session),
            "orders": sorted(RATING_ORDERS),
            "page_title": "评分队列",
        },
    )


@router.post("/queues")
def rating_create(payload: RatingQueueCreatePayload, request: Request) -> dict[str, object]:
    if payload.allow_network:
        if payload.max_items is None or payload.max_items > 200:
            raise HTTPException(
                status_code=409,
                detail="Network-enriched rating queues require max_items no greater than 200",
            )
        with request.app.state.session_factory.begin() as session:
            job = enqueue_job(
                session,
                kind="rating_queue_create_enriched",
                capability="remote_read",
                config=payload.model_dump(exclude={"allow_network"}),
            )
            job_id = job.id
        return {
            "status": "pending_external_worker",
            "job_id": job_id,
            "operation": "rating_queue_create_enriched",
            "selector": payload.model_dump(exclude={"allow_network"}),
            "network_permitted": True,
        }
    try:
        with request.app.state.session_factory.begin() as session:
            seeds = prepare_rating_queue(
                session,
                subject_types=tuple(payload.subject_types),
                collection_statuses=tuple(payload.collection_statuses),
                include_deferred=payload.include_deferred,
                order_name=payload.order,
                random_seed=payload.seed,
                max_items=payload.max_items,
            )
            view = create_rating_queue(
                session,
                seeds,
                selector={
                    "subject_types": payload.subject_types,
                    "collection_statuses": payload.collection_statuses,
                    "include_deferred": payload.include_deferred,
                    "max_items": payload.max_items,
                    "allow_network": False,
                },
                order_name=payload.order,
                random_seed=payload.seed,
            )
            session_id = view.session.id
    except RatingQueueError as exc:
        raise _error(exc) from None
    return {"session_id": session_id, "status": "active", "network_requests": 0}


@router.get("/queues/{session_id}", response_class=HTMLResponse)
def rating_show(
    session_id: str,
    request: Request,
    position: int | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    try:
        view = load_rating_queue(session, session_id)
        counts = rating_queue_counts(session, session_id)
    except RatingQueueError as exc:
        raise _error(exc, 404) from None
    items = []
    for item in view.items:
        snapshot = json.loads(item.subject_snapshot_json)
        initial = json.loads(item.initial_snapshot_json)
        collection_type = initial.get("type")
        try:
            kind = SubjectType.parse(snapshot.get("subject_type")).kind
        except (TypeError, ValueError):
            kind = None
        items.append(
            {
                "row": item,
                "snapshot": snapshot,
                "initial": initial,
                "collection_status_label": (
                    status_label(collection_type, kind)
                    if isinstance(collection_type, int)
                    else "尚未收藏"
                ),
                "cover_src": local_media_url(
                    session,
                    request.app.state.settings.media_cache_directory,
                    rating_queue_item_id=item.id,
                    subject_id=item.subject_id,
                ),
            }
        )
    if items:
        if position is None:
            pending = next(
                (index for index, item in enumerate(items) if item["row"].item_status == "pending"),
                min(view.session.cursor_position, len(items) - 1),
            )
            selected_index = pending
        else:
            selected_index = max(0, min(position - 1, len(items) - 1))
        selected = items[selected_index]
    else:
        selected_index = 0
        selected = None
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="rating/detail.html",
        context={
            "queue": view.session,
            "item": selected,
            "selected_position": selected_index + 1,
            "previous_position": selected_index if selected_index > 0 else None,
            "next_position": selected_index + 2 if selected_index + 1 < len(items) else None,
            "counts": counts,
            "queue_status_label": status_label(view.session.status),
            "item_status_label": status_label(selected["row"].item_status) if selected else "",
            "item_outcome_label": status_label(selected["row"].outcome) if selected and selected["row"].outcome else "",
            "page_title": f"评分队列 {session_id}",
        },
    )


@router.post("/queues/{session_id}/enrich", status_code=202)
def rating_enrich_job(
    session_id: str,
    payload: RatingEnrichmentPayload,
    request: Request,
) -> dict[str, object]:
    try:
        with request.app.state.session_factory.begin() as session:
            view = load_rating_queue(session, session_id)
            if payload.scope == "current":
                if payload.subject_id is None:
                    raise HTTPException(422, "subject_id is required for current enrichment")
                selected = [item for item in view.items if item.subject_id == payload.subject_id]
                if not selected:
                    raise HTTPException(404, "Subject is not part of this rating queue")
            else:
                selected = list(view.items)
            if not selected:
                raise HTTPException(409, "Rating queue has no items to enrich")
            if len(selected) > 200:
                raise HTTPException(409, "Queue enrichment is limited to 200 items")
            job = enqueue_job(
                session,
                kind="rating_queue_enrich",
                capability="remote_read",
                config={
                    "session_id": session_id,
                    "subject_ids": [item.subject_id for item in selected],
                    "cache_image": payload.cache_image,
                },
            )
            job_id = job.id
    except RatingQueueError as exc:
        raise _error(exc, 404) from None
    return {
        "status": "pending_external_worker",
        "job_id": job_id,
        "operation": "rating_queue_enrich",
        "item_count": len(selected),
    }


@router.get("/queues/{session_id}/next")
def rating_next(
    session_id: str, session: Session = Depends(get_session)
) -> dict[str, object]:
    try:
        item = next_rating_item(session, session_id)
    except RatingQueueError as exc:
        raise _error(exc, 404) from None
    if item is None:
        return {"completed": True, "item": None}
    return {
        "completed": False,
        "item": {
            "item_id": item.id,
            "subject_id": item.subject_id,
            "position": item.position,
            "snapshot": json.loads(item.subject_snapshot_json),
        },
    }


@router.post("/queues/{session_id}/subjects/{subject_id}/rate")
def rating_rate(
    session_id: str,
    subject_id: int,
    payload: RatingActionPayload,
    request: Request,
) -> dict[str, object]:
    stale_error: str | None = None
    try:
        with request.app.state.session_factory.begin() as session:
            try:
                item = rate_rating_item(
                    session,
                    session_id,
                    subject_id,
                    score=payload.score,
                    reason=payload.reason,
                    skip_reason=payload.skip_reason,
                    publish_reason=payload.publish_reason,
                    public_comment=payload.public_comment,
                    replace_existing_comment=payload.replace_existing_comment,
                )
                outcome = item.outcome
            except RatingQueueStale as exc:
                stale_error = str(exc)
                outcome = "stale"
    except RatingQueueError as exc:
        raise _error(exc) from None
    if stale_error:
        raise HTTPException(status_code=409, detail=stale_error)
    # Private reason is intentionally absent from the response.
    return {"subject_id": subject_id, "outcome": outcome, "remote_writes": 0}


def _rating_disposition(
    session_id: str,
    subject_id: int,
    payload: PrivateReasonPayload,
    request: Request,
    decision: str,
) -> dict[str, object]:
    stale_error: str | None = None
    try:
        with request.app.state.session_factory.begin() as session:
            try:
                item = set_rating_disposition(
                    session,
                    session_id,
                    subject_id,
                    decision=decision,
                    reason=payload.reason,
                )
                outcome = item.outcome
            except RatingQueueStale as exc:
                stale_error = str(exc)
                outcome = "stale"
    except RatingQueueError as exc:
        raise _error(exc) from None
    if stale_error:
        raise HTTPException(status_code=409, detail=stale_error)
    return {"subject_id": subject_id, "outcome": outcome, "remote_writes": 0}


@router.post("/queues/{session_id}/subjects/{subject_id}/skip")
def rating_skip(
    session_id: str, subject_id: int, payload: PrivateReasonPayload, request: Request
) -> dict[str, object]:
    return _rating_disposition(session_id, subject_id, payload, request, "skipped")


@router.post("/queues/{session_id}/subjects/{subject_id}/defer")
def rating_defer(
    session_id: str, subject_id: int, payload: PrivateReasonPayload, request: Request
) -> dict[str, object]:
    return _rating_disposition(session_id, subject_id, payload, request, "deferred")


@router.post("/subjects/{subject_id}/reopen")
def rating_reopen(
    subject_id: int, payload: PrivateReasonPayload, request: Request
) -> dict[str, object]:
    try:
        with request.app.state.session_factory.begin() as session:
            state = reopen_rating_subject(session, subject_id, payload.reason)
            version = state.version
    except RatingQueueError as exc:
        raise _error(exc, 404) from None
    return {"subject_id": subject_id, "state": "pending", "version": version}


@router.post("/queues/{session_id}/sync-plan", status_code=202)
def rating_sync_plan_job(session_id: str, request: Request) -> dict[str, object]:
    try:
        with request.app.state.session_factory.begin() as session:
            subject_ids = rated_subject_ids(session, session_id)
            job = enqueue_job(
                session,
                kind="rating_sync_plan",
                capability="remote_read",
                config={
                    "session_id": session_id,
                    "subject_ids": list(subject_ids),
                    "fields": ["rate", "comment"],
                },
            )
            job_id = job.id
    except RatingQueueError as exc:
        raise _error(exc, 404) from None
    # Fresh remote reads and plan persistence are performed by the external worker.
    return {
        "status": "pending_external_worker",
        "job_id": job_id,
        "operation": "rating_sync_plan",
        "session_id": session_id,
        "subject_ids": list(subject_ids),
        "fields": ["rate", "comment"],
        "requires_fresh_remote": True,
    }

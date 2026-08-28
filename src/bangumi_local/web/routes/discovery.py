from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from bangumi_local.domain.models import CollectionStatus
from bangumi_local.services.discovery import (
    DISCOVERY_DECISIONS,
    DiscoveryError,
    DiscoveryIdentityConflict,
    create_discovery_session,
    delete_discovery_session,
    decide_discovery_candidate,
    list_discovery_sessions,
    load_discovery_session,
    next_discovery_candidate,
    promotion_preview,
    reopen_discovery_candidate,
    steam_discovery_seeds,
)
from bangumi_local.services.backups import BackupError, backup_sqlite_database
from bangumi_local.services.steam_library import SteamLibraryError
from bangumi_local.services.jobs import enqueue_job
from bangumi_local.web.dependencies import get_session
from bangumi_local.web.routes.media import local_media_url


router = APIRouter(prefix="/discovery")


class SteamDiscoveryPayload(BaseModel):
    account_id: str | None = None
    include_owned_unplayed: bool = False
    include_decided: bool = False
    max_items: int = Field(default=50, ge=1, le=200)


class SearchDiscoveryPayload(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    public_tags: list[str] = Field(default_factory=list, max_length=20)
    year_from: int | None = Field(default=None, ge=1900, le=2200)
    year_to: int | None = Field(default=None, ge=1900, le=2200)
    min_rating_count: int | None = Field(default=None, ge=0)
    sort: str = "match"
    max_items: int = Field(default=50, ge=1, le=200)
    allow_network: bool = False
    cache_images: bool = False


class BrowseDiscoveryPayload(BaseModel):
    year: int | None = Field(default=None, ge=1900, le=2200)
    platform: str | None = Field(default=None, max_length=100)
    sort: str = "rank"
    max_items: int = Field(default=50, ge=1, le=200)
    allow_network: bool = False
    cache_images: bool = False

    @model_validator(mode="after")
    def bounded(self) -> BrowseDiscoveryPayload:
        if self.year is None and not (self.platform and self.platform.strip()):
            raise ValueError("A year or platform is required")
        return self


class DiscoveryDecisionPayload(BaseModel):
    decision: str
    reason: str | None = Field(default=None, max_length=10000)


class DiscoveryReopenPayload(BaseModel):
    candidate_key: str = Field(min_length=1, max_length=500)
    reason: str | None = Field(default=None, max_length=10000)


class IdentityPayload(BaseModel):
    subject_id: int | None = Field(default=None, ge=1)
    allow_network: bool = False


class StatusDraftPayload(BaseModel):
    status: str
    allow_network: bool = False


class DiscoveryImagePayload(BaseModel):
    scope: Literal["current", "session"] = "current"
    candidate_id: str | None = Field(default=None, max_length=36)


class DiscoveryDeletePayload(BaseModel):
    confirm_session_id: str = Field(min_length=36, max_length=36)


def _error(exc: Exception, status: int = 409) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


@router.get("", response_class=HTMLResponse)
def discovery_list(
    request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="discovery/list.html",
        context={
            "sessions": list_discovery_sessions(session),
            "page_title": "探索队列",
        },
    )


@router.post("/sessions/steam")
def discovery_create_steam(payload: SteamDiscoveryPayload, request: Request) -> dict[str, object]:
    try:
        with request.app.state.session_factory.begin() as session:
            seeds = steam_discovery_seeds(
                session,
                account_id=payload.account_id,
                include_owned_unplayed=payload.include_owned_unplayed,
                include_decided=payload.include_decided,
                max_items=payload.max_items,
            )
            view = create_discovery_session(
                session,
                provider="steam",
                filters=payload.model_dump(),
                seeds=seeds,
            )
            session_id = view.session.id
            count = len(view.candidates)
    except (DiscoveryError, SteamLibraryError) as exc:
        raise _error(exc) from None
    return {"session_id": session_id, "candidate_count": count, "network_requests": 0}


@router.post("/sessions/search", status_code=202)
def discovery_create_search_job(
    payload: SearchDiscoveryPayload, request: Request
) -> dict[str, object]:
    if not payload.allow_network:
        raise HTTPException(status_code=409, detail="Explicit network permission is required")
    if not payload.query.strip():
        raise HTTPException(status_code=422, detail="Search query must not be empty")
    config = payload.model_dump(exclude={"allow_network"})
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="discovery_create_search",
            capability="remote_read",
            config=config,
        )
        job_id = job.id
    return {
        "status": "pending_external_worker",
        "job_id": job_id,
        "operation": "discovery_create_search",
        "filters": config,
        "subject_type": "game",
        "include_nsfw": False,
    }


@router.post("/sessions/browse", status_code=202)
def discovery_create_browse_job(
    payload: BrowseDiscoveryPayload, request: Request
) -> dict[str, object]:
    if not payload.allow_network:
        raise HTTPException(status_code=409, detail="Explicit network permission is required")
    config = payload.model_dump(exclude={"allow_network"})
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="discovery_create_browse",
            capability="remote_read",
            config=config,
        )
        job_id = job.id
    return {
        "status": "pending_external_worker",
        "job_id": job_id,
        "operation": "discovery_create_browse",
        "filters": config,
        "subject_type": "game",
        "include_nsfw": False,
    }


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
def discovery_show(
    session_id: str,
    request: Request,
    position: int | None = None,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    try:
        view = load_discovery_session(session, session_id)
    except DiscoveryError as exc:
        raise _error(exc, 404) from None
    candidates = [
        {
            "row": item,
            "tags": json.loads(item.public_tags_json),
            "evidence": json.loads(item.evidence_json),
            "cover_src": local_media_url(
                session,
                request.app.state.settings.media_cache_directory,
                discovery_candidate_id=item.id,
                work_id=item.work_id,
                subject_id=item.subject_id,
                library_entry_id=item.library_entry_id,
            ),
        }
        for item in view.candidates
    ]
    if candidates:
        if position is None:
            selected_index = next(
                (
                    index
                    for index, item in enumerate(candidates)
                    if item["row"].item_status == "pending"
                ),
                min(view.session.cursor_position, len(candidates) - 1),
            )
        else:
            selected_index = max(0, min(position - 1, len(candidates) - 1))
        selected = candidates[selected_index]
    else:
        selected_index = 0
        selected = None
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="discovery/detail.html",
        context={
            "discovery": view.session,
            "candidate": selected,
            "selected_position": selected_index + 1,
            "previous_position": selected_index if selected_index > 0 else None,
            "next_position": (
                selected_index + 2 if selected_index + 1 < len(candidates) else None
            ),
            "decisions": sorted(DISCOVERY_DECISIONS),
            "page_title": f"探索会话 {session_id}",
        },
    )


@router.post("/sessions/{session_id}/cache-images", status_code=202)
def discovery_cache_images_job(
    session_id: str,
    payload: DiscoveryImagePayload,
    request: Request,
) -> dict[str, object]:
    try:
        with request.app.state.session_factory.begin() as session:
            view = load_discovery_session(session, session_id)
            if payload.scope == "current":
                if payload.candidate_id is None:
                    raise HTTPException(422, "candidate_id is required for current caching")
                selected = [item for item in view.candidates if item.id == payload.candidate_id]
                if not selected:
                    raise HTTPException(404, "Candidate is not part of this discovery session")
            else:
                selected = list(view.candidates)
            if not selected:
                raise HTTPException(409, "Discovery session has no candidates")
            if len(selected) > 200:
                raise HTTPException(409, "Discovery image caching is limited to 200 items")
            job = enqueue_job(
                session,
                kind="discovery_cache_images",
                capability="remote_read",
                config={
                    "session_id": session_id,
                    "candidate_ids": [item.id for item in selected],
                },
            )
            job_id = job.id
    except DiscoveryError as exc:
        raise _error(exc, 404) from None
    return {
        "status": "pending_external_worker",
        "job_id": job_id,
        "operation": "discovery_cache_images",
        "item_count": len(selected),
    }


@router.post("/sessions/{session_id}/delete")
def discovery_delete(
    session_id: str,
    payload: DiscoveryDeletePayload,
    request: Request,
) -> dict[str, object]:
    if payload.confirm_session_id != session_id:
        raise HTTPException(422, "Type the complete session ID to confirm deletion")
    settings = request.app.state.settings
    try:
        with request.app.state.session_factory() as session:
            load_discovery_session(session, session_id)
    except DiscoveryError as exc:
        raise _error(exc, 404) from None
    try:
        backup_sqlite_database(
            settings.database_url,
            settings.backup_directory,
            label=f"before-discovery-delete-{session_id}",
        )
    except BackupError as exc:
        raise HTTPException(409, str(exc)) from None
    try:
        with request.app.state.session_factory.begin() as session:
            result = delete_discovery_session(session, session_id)
    except DiscoveryError as exc:
        raise _error(exc, 404) from None
    return {
        "deleted": True,
        "session_id": result.session_id,
        "candidate_count": result.candidate_count,
        "backup_created": True,
        "global_decisions_preserved": True,
    }


@router.get("/sessions/{session_id}/next")
def discovery_next(
    session_id: str, request: Request, session: Session = Depends(get_session)
) -> dict[str, object]:
    try:
        candidate = next_discovery_candidate(session, session_id)
    except DiscoveryError as exc:
        raise _error(exc, 404) from None
    if candidate is None:
        return {"completed": True, "candidate": None}
    return {
        "completed": False,
        "candidate": {
            "id": candidate.id,
            "title": candidate.title,
            "position": candidate.position,
            "priority": candidate.priority_score,
            "cover_url": local_media_url(
                session,
                request.app.state.settings.media_cache_directory,
                discovery_candidate_id=candidate.id,
                work_id=candidate.work_id,
                subject_id=candidate.subject_id,
                library_entry_id=candidate.library_entry_id,
            ),
        },
    }


@router.post("/sessions/{session_id}/candidates/{candidate_id}/decide")
def discovery_decide(
    session_id: str,
    candidate_id: str,
    payload: DiscoveryDecisionPayload,
    request: Request,
) -> dict[str, object]:
    if payload.decision not in DISCOVERY_DECISIONS:
        raise HTTPException(status_code=422, detail="Unsupported discovery decision")
    conflict: str | None = None
    try:
        with request.app.state.session_factory.begin() as session:
            try:
                candidate = decide_discovery_candidate(
                    session,
                    session_id,
                    candidate_id,
                    decision=payload.decision,
                    reason=payload.reason,
                )
                decision = candidate.decision
            except DiscoveryIdentityConflict as exc:
                conflict = str(exc)
                decision = None
    except DiscoveryError as exc:
        raise _error(exc) from None
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)
    # Private reason is deliberately omitted.
    return {"candidate_id": candidate_id, "decision": decision, "remote_writes": 0}


@router.post("/reopen")
def discovery_reopen(payload: DiscoveryReopenPayload, request: Request) -> dict[str, object]:
    try:
        with request.app.state.session_factory.begin() as session:
            review = reopen_discovery_candidate(
                session, payload.candidate_key, reason=payload.reason
            )
            key = review.candidate_key
    except DiscoveryError as exc:
        raise _error(exc, 404) from None
    return {"candidate_key": key, "decision": None}


@router.get("/candidates/{candidate_id}/promotion")
def discovery_promotion_preview(
    candidate_id: str, request: Request, session: Session = Depends(get_session)
) -> object:
    try:
        preview = promotion_preview(session, candidate_id)
    except DiscoveryError as exc:
        raise _error(exc, 404) from None
    payload = {
        "candidate_id": preview.candidate_id,
        "status": preview.status,
        "work_id": preview.work_id,
        "subject_id": preview.subject_id,
        "library_entry_id": preview.library_entry_id,
        "detail": preview.detail,
        "mutation_performed": False,
    }
    if "text/html" in request.headers.get("accept", ""):
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="discovery/promotion.html",
            context={
                "page_title": "Discovery Promotion",
                "preview": preview,
                "statuses": tuple(item.label for item in CollectionStatus),
            },
        )
    return payload


@router.post("/candidates/{candidate_id}/identity", status_code=202)
def discovery_identity_job(
    candidate_id: str, payload: IdentityPayload, request: Request
) -> dict[str, object]:
    if not payload.allow_network:
        raise HTTPException(
            status_code=409, detail="Fresh Bangumi identity verification is required"
        )
    with request.app.state.session_factory.begin() as session:
        try:
            preview = promotion_preview(session, candidate_id)
        except DiscoveryError as exc:
            raise _error(exc, 404) from None
        if preview.status == "identity_conflict":
            raise HTTPException(status_code=409, detail="Identity conflict requires review")
        if preview.status == "already_collected":
            raise HTTPException(status_code=409, detail="Candidate is already collected")
        if preview.status == "needs_steam_match" and payload.subject_id is None:
            raise HTTPException(status_code=409, detail="A Bangumi subject ID is required")
        job = enqueue_job(
            session,
            kind="discovery_promote_identity",
            capability="remote_read",
            config={"candidate_id": candidate_id, "subject_id": payload.subject_id},
        )
        job_id = job.id
    return {
        "status": "pending_external_worker",
        "job_id": job_id,
        "operation": "discovery_promote_identity",
        "candidate_id": candidate_id,
        "subject_id": payload.subject_id,
        "requires_fresh_subject": True,
    }


@router.post("/candidates/{candidate_id}/status-draft", status_code=202)
def discovery_status_draft_job(
    candidate_id: str, payload: StatusDraftPayload, request: Request
) -> dict[str, object]:
    status_by_label = {item.label: item for item in CollectionStatus}
    if payload.status not in status_by_label:
        raise HTTPException(status_code=422, detail="Unsupported Bangumi collection status")
    if not payload.allow_network:
        raise HTTPException(status_code=409, detail="Fresh remote collection read is required")
    with request.app.state.session_factory.begin() as session:
        try:
            preview = promotion_preview(session, candidate_id)
        except DiscoveryError as exc:
            raise _error(exc, 404) from None
        if preview.status not in {"existing_identity", "already_collected"}:
            raise HTTPException(
                status_code=409,
                detail="Confirm or promote an identity before creating a status draft",
            )
        job = enqueue_job(
            session,
            kind="discovery_status_draft",
            capability="remote_read",
            config={"candidate_id": candidate_id, "status": payload.status},
        )
        job_id = job.id
    return {
        "status": "pending_external_worker",
        "job_id": job_id,
        "operation": "discovery_status_draft",
        "candidate_id": candidate_id,
        "explicit_collection_status": payload.status,
        "requires_fresh_remote": True,
        "inferred_from_played": False,
    }

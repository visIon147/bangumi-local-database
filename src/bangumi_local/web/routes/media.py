from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    BangumiSubject,
    LibraryEntry,
    MediaBinding,
    MediaBlob,
    MediaSource,
)
from bangumi_local.domain.media import ImagePolicy
from bangumi_local.services.media import (
    MediaError,
    media_status,
    scan_steam_librarycache,
)
from bangumi_local.services.jobs import enqueue_job
from bangumi_local.web.dependencies import get_session, get_settings


router = APIRouter(prefix="/media")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MediaScanPayload(BaseModel):
    app_ids: list[str] | None = None
    policy: ImagePolicy = ImagePolicy.METADATA


class MediaFetchPayload(BaseModel):
    source_ids: list[str] = Field(default_factory=list, max_length=200)
    allow_network: bool = False

_PLACEHOLDER = b"""<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"320\" height=\"420\" viewBox=\"0 0 320 420\"><rect width=\"320\" height=\"420\" fill=\"#202938\"/><path d=\"M90 265l55-70 42 48 28-33 34 55z\" fill=\"#64748b\"/><circle cx=\"112\" cy=\"130\" r=\"24\" fill=\"#94a3b8\"/><text x=\"160\" y=\"330\" text-anchor=\"middle\" fill=\"#cbd5e1\" font-family=\"sans-serif\" font-size=\"18\">No image</text></svg>"""


@router.get("/placeholder")
def media_placeholder() -> Response:
    return Response(
        _PLACEHOLDER,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _registered_blob_path(cache_directory: Path, blob: MediaBlob) -> Path:
    root = cache_directory.expanduser().resolve()
    target = (root / blob.storage_relpath).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="Cached media is unavailable")
    return target


def _local_blob_url(
    session: Session, cache_directory: Path, digest: str | None
) -> str | None:
    if digest is None or not _SHA256.fullmatch(digest):
        return None
    blob = session.get(MediaBlob, digest)
    if blob is None:
        return None
    try:
        _registered_blob_path(cache_directory, blob)
    except HTTPException:
        return None
    return f"/media/blob/{digest}"


def local_media_url(
    session: Session,
    cache_directory: Path,
    *,
    rating_queue_item_id: str | None = None,
    discovery_candidate_id: str | None = None,
    work_id: int | None = None,
    subject_id: int | None = None,
    library_entry_id: int | None = None,
) -> str:
    """Resolve one already-cached image without performing filesystem discovery or network I/O."""

    if subject_id is not None and work_id is None:
        identity = session.get(BangumiSubject, subject_id)
        work_id = identity.work_id if identity is not None else None

    binding_scopes = (
        (MediaBinding.rating_queue_item_id, rating_queue_item_id),
        (MediaBinding.discovery_candidate_id, discovery_candidate_id),
        (MediaBinding.work_id, work_id),
        (MediaBinding.library_entry_id, library_entry_id),
    )
    for column, value in binding_scopes:
        if value is None:
            continue
        rows = session.execute(
            select(MediaBinding, MediaSource)
            .join(MediaSource, MediaSource.id == MediaBinding.media_source_id)
            .where(column == value)
            .order_by(MediaBinding.priority.desc(), MediaBinding.last_observed_at.desc())
        ).all()
        for binding, source in rows:
            url = _local_blob_url(
                session,
                cache_directory,
                binding.pinned_blob_sha256 or source.current_blob_sha256,
            )
            if url is not None:
                return url

    # Metadata may have been cached before an entity binding was created.
    external_keys: list[tuple[str, str]] = []
    if subject_id is not None:
        external_keys.append(("bangumi", str(subject_id)))
    if work_id is not None:
        identity = session.scalar(
            select(BangumiSubject).where(BangumiSubject.work_id == work_id)
        )
        if identity is not None:
            external_keys.append(("bangumi", str(identity.subject_id)))
        for app_id in session.scalars(
            select(LibraryEntry.external_id).where(LibraryEntry.work_id == work_id)
        ):
            external_keys.append(("steam", app_id))
    if library_entry_id is not None:
        entry = session.get(LibraryEntry, library_entry_id)
        if entry is not None:
            external_keys.append(("steam", entry.external_id))

    variant_priority = {
        "library_portrait": 100,
        "library_capsule": 90,
        "common": 80,
        "large": 70,
        "medium": 60,
        "header": 50,
        "library_header": 40,
        "small": 30,
        "grid": 20,
        "hero": 10,
    }
    seen: set[tuple[str, str]] = set()
    for provider, external_id in external_keys:
        if (provider, external_id) in seen:
            continue
        seen.add((provider, external_id))
        sources = list(
            session.scalars(
                select(MediaSource).where(
                    MediaSource.provider == provider,
                    MediaSource.external_id == external_id,
                    MediaSource.current_blob_sha256.is_not(None),
                )
            )
        )
        sources.sort(
            key=lambda item: (variant_priority.get(item.variant, 0), item.observed_at),
            reverse=True,
        )
        for source in sources:
            url = _local_blob_url(session, cache_directory, source.current_blob_sha256)
            if url is not None:
                return url
    return "/media/placeholder"


@router.get("", response_class=HTMLResponse)
def media_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    # Rendering is local-only and never scans Steam or fetches remote URLs.
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="media/status.html",
        context={"summary": media_status(session), "page_title": "图片缓存"},
    )


@router.get("/status")
def media_status_json(session: Session = Depends(get_session)) -> dict[str, int]:
    summary = media_status(session)
    return {
        "sources": summary.source_count,
        "cached_sources": summary.cached_source_count,
        "failed_sources": summary.failed_source_count,
        "missing_sources": summary.missing_source_count,
        "blobs": summary.blob_count,
        "bytes": summary.total_bytes,
    }


@router.get("/blob/{sha256}", name="media_blob")
def media_blob(
    sha256: str,
    session: Session = Depends(get_session),
    settings=Depends(get_settings),
) -> FileResponse:
    if not _SHA256.fullmatch(sha256):
        raise HTTPException(status_code=404, detail="Cached media not found")
    blob = session.get(MediaBlob, sha256)
    if blob is None:
        raise HTTPException(status_code=404, detail="Cached media not found")
    return FileResponse(
        _registered_blob_path(settings.media_cache_directory, blob),
        media_type=blob.mime_type,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "ETag": f'"{blob.sha256}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/scan-preview")
def media_scan_preview(
    payload: MediaScanPayload, settings=Depends(get_settings)
) -> dict[str, object]:
    if payload.policy is ImagePolicy.NONE:
        return {
            "operation": "steam_media_scan",
            "policy": "none",
            "candidate_count": 0,
            "app_count": 0,
            "network_requests": 0,
        }
    if settings.steam_root is None:
        raise HTTPException(status_code=409, detail="Steam root is not configured")
    try:
        candidates = scan_steam_librarycache(settings.steam_root, app_ids=payload.app_ids)
    except MediaError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {
        "operation": "steam_media_scan",
        "policy": payload.policy.value,
        "candidate_count": len(candidates),
        "app_count": len({item.reference.external_id for item in candidates}),
        "network_requests": 0,
    }


@router.post("/scan", status_code=202)
def media_scan(payload: MediaScanPayload, request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    if payload.policy is ImagePolicy.NONE:
        return {
            "examined": 0,
            "created": 0,
            "updated": 0,
            "cached": 0,
            "skipped": 0,
            "network_requests": 0,
        }
    if settings.steam_root is None:
        raise HTTPException(status_code=409, detail="Steam root is not configured")
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="steam_media_scan",
            capability="local_write",
            config={"app_ids": payload.app_ids or [], "policy": payload.policy.value},
        )
    return {
        "job_id": job.id,
        "status": "queued",
        "operation": "steam_media_scan",
        "network_requests": 0,
    }


@router.post("/verify")
def media_verify_job(request: Request) -> RedirectResponse:
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session, kind="media_verify", capability="local_read", config={}
        )
        job_id = job.id
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/prune")
def media_prune_job(
    request: Request,
    max_bytes: int | None = Form(None),
    confirm: str = Form(""),
) -> RedirectResponse:
    if confirm != "PRUNE":
        raise HTTPException(422, "Type PRUNE to confirm local cache eviction")
    if max_bytes is not None and max_bytes < 0:
        raise HTTPException(422, "max_bytes must be non-negative")
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="media_prune",
            capability="local_write",
            config={"max_bytes": max_bytes},
        )
        job_id = job.id
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/fetch-preview")
def media_fetch_preview(
    payload: MediaFetchPayload, session: Session = Depends(get_session)
) -> dict[str, object]:
    statement = select(MediaSource).where(MediaSource.origin == "remote")
    if payload.source_ids:
        statement = statement.where(MediaSource.id.in_(payload.source_ids))
    sources = session.scalars(statement.limit(200)).all()
    return {
        "operation": "remote_media_fetch",
        "selected": len(sources),
        "already_cached": sum(item.current_blob_sha256 is not None for item in sources),
        "requires_allow_network": True,
        "performed": False,
    }


@router.post("/fetch", status_code=202)
def media_fetch_job(payload: MediaFetchPayload, request: Request) -> dict[str, object]:
    if not payload.allow_network:
        raise HTTPException(status_code=409, detail="Explicit network permission is required")
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="remote_media_fetch",
            capability="remote_read",
            config={"source_ids": payload.source_ids},
        )
        job_id = job.id
    # A worker consumes this sanitized DTO outside request/database transactions.
    return {
        "status": "pending_external_worker",
        "job_id": job_id,
        "operation": "remote_media_fetch",
        "source_ids": payload.source_ids,
        "network_permitted": True,
    }

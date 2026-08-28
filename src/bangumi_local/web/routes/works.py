from __future__ import annotations

from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    LibraryEntry,
    MediaBinding,
    MediaSource,
    RemoteOperation,
    Tag,
    Work,
    WorkLink,
    WorkTag,
)
from bangumi_local.domain.models import CollectionStatus
from bangumi_local.services.read_models import (
    ReadModelError,
    list_works as list_work_summaries,
    personal_tag_facets,
)
from bangumi_local.web.dependencies import get_session
from bangumi_local.services.game_profiles import (
    GameProfileError,
    GameProfilePatch,
    edit_game_profile,
)


router = APIRouter(prefix="/works")
KINDS = {"book", "anime", "music", "game", "real", "unknown"}
KIND_LABELS = {
    "book": "书籍",
    "anime": "动画",
    "music": "音乐",
    "game": "游戏",
    "real": "三次元",
    "unknown": "Unknown",
}
PAGE_SIZES = (12, 24, 48, 96)


def _local_cover(session: Session, work_id: int) -> str:
    digest = session.scalar(
        select(MediaSource.current_blob_sha256)
        .join(MediaBinding, MediaBinding.media_source_id == MediaSource.id)
        .outerjoin(LibraryEntry, LibraryEntry.id == MediaBinding.library_entry_id)
        .where(
            MediaSource.current_blob_sha256.is_not(None),
            MediaBinding.role == "cover",
            or_(MediaBinding.work_id == work_id, LibraryEntry.work_id == work_id),
        )
        .order_by(MediaBinding.priority.desc(), MediaBinding.last_observed_at.desc())
        .limit(1)
    )
    return f"/media/blob/{digest}" if digest else "/media/placeholder"


def _safe_external_url(value: str) -> str | None:
    parsed = urlsplit(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


@router.get("", response_class=HTMLResponse)
def works_list(
    request: Request,
    q: str | None = Query(None, max_length=200),
    kind: str | None = Query(None),
    source: str = Query("all"),
    collection_status: list[int] | None = Query(None),
    tag_include: list[str] | None = Query(None),
    tag_include_mode: str = Query("all"),
    tag_exclude: list[str] | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(24),
    session: Session = Depends(get_session),
) -> Response:
    normalized_query = q.strip() if q else None
    normalized_kind = kind if kind in KINDS else None
    if kind and normalized_kind is None:
        raise HTTPException(422, "Unsupported work kind.")
    if source not in {"all", "bangumi", "steam"}:
        raise HTTPException(422, "Unsupported work source.")
    if tag_include_mode not in {"all", "any"}:
        raise HTTPException(422, "Tag include mode must be all or any.")
    if page_size not in PAGE_SIZES:
        raise HTTPException(422, "Unsupported page size.")
    valid_statuses = {int(value) for value in CollectionStatus}
    normalized_statuses = tuple(dict.fromkeys(collection_status or ()))
    if any(value not in valid_statuses for value in normalized_statuses):
        raise HTTPException(422, "Unsupported collection status.")
    normalized_included = tuple(
        dict.fromkeys(value.strip() for value in (tag_include or ()) if value.strip())
    )
    normalized_excluded = tuple(
        dict.fromkeys(value.strip() for value in (tag_exclude or ()) if value.strip())
    )
    try:
        result = list_work_summaries(
            session,
            page=page,
            page_size=page_size,
            query=normalized_query,
            kinds=(normalized_kind,) if normalized_kind else (),
            collection_statuses=normalized_statuses,
            tags=normalized_included,
            tag_match=tag_include_mode,
            exclude_tags=normalized_excluded,
            source=source,
        )
    except ReadModelError as exc:
        raise HTTPException(422, str(exc)) from None

    query_pairs: list[tuple[str, str | int]] = [("page_size", page_size)]
    if normalized_query:
        query_pairs.append(("q", normalized_query))
    if normalized_kind:
        query_pairs.append(("kind", normalized_kind))
    if source != "all":
        query_pairs.append(("source", source))
    query_pairs.extend(("collection_status", value) for value in normalized_statuses)
    query_pairs.extend(("tag_include", value) for value in normalized_included)
    if normalized_included and tag_include_mode != "all":
        query_pairs.append(("tag_include_mode", tag_include_mode))
    query_pairs.extend(("tag_exclude", value) for value in normalized_excluded)

    def page_url(target: int) -> str:
        return "/works?" + urlencode([*query_pairs, ("page", target)])

    if result.total and page > result.page_count:
        return RedirectResponse(page_url(result.page_count), status_code=303)

    items = [
        {
            "row": row,
            "cover_src": _local_cover(session, row.work_id),
        }
        for row in result.items
    ]
    page_count = max(1, result.page_count)
    start = max(1, min(page - 2, page_count - 4))
    end = min(page_count, start + 4)
    page_links = tuple((number, page_url(number)) for number in range(start, end + 1))
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="works/list.html",
        context={
            "items": items,
            "q": normalized_query or "",
            "kind": normalized_kind or "",
            "kinds": tuple((value, KIND_LABELS[value]) for value in KIND_LABELS),
            "source": source,
            "collection_statuses": tuple(
                (int(value), value.label) for value in CollectionStatus
            ),
            "selected_statuses": normalized_statuses,
            "personal_tags": personal_tag_facets(session),
            "included_tags": normalized_included,
            "excluded_tags": normalized_excluded,
            "tag_include_mode": tag_include_mode,
            "page": page,
            "page_size": page_size,
            "page_sizes": PAGE_SIZES,
            "total": result.total,
            "page_count": page_count,
            "page_links": page_links,
            "previous_url": page_url(page - 1) if page > 1 else None,
            "next_url": page_url(page + 1) if page < result.page_count else None,
            "filter_pairs": query_pairs,
            "has_previous": page > 1,
            "has_next": page < result.page_count,
            "page_title": "作品",
        },
    )


@router.get("/{work_id}", response_class=HTMLResponse)
def work_detail(
    work_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    work = session.get(Work, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="Work not found")
    subject = session.scalar(
        select(BangumiSubject).where(BangumiSubject.work_id == work_id)
    )
    collection = (
        session.get(BangumiCollectionState, subject.subject_id)
        if subject is not None
        else None
    )
    tags = session.scalars(
        select(Tag)
        .join(WorkTag, WorkTag.tag_id == Tag.id)
        .where(WorkTag.work_id == work_id)
        .order_by(Tag.name)
    ).all()
    stored_links = session.scalars(
        select(WorkLink).where(WorkLink.work_id == work_id).order_by(WorkLink.source)
    ).all()
    links = [
        {"source": item.source, "url": safe_url}
        for item in stored_links
        if (safe_url := _safe_external_url(item.url)) is not None
    ]
    operations = (
        session.scalars(
            select(RemoteOperation)
            .where(RemoteOperation.subject_id == subject.subject_id)
            .order_by(RemoteOperation.started_at.desc())
            .limit(20)
        ).all()
        if subject is not None
        else []
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="works/detail.html",
        context={
            "work": work,
            "subject": subject,
            "collection": collection,
            "tags": tags,
            "links": links,
            "operations": operations,
            "profile": work.game_profile,
            "cover_src": _local_cover(session, work.id),
            "page_title": work.title,
        },
    )


@router.post("/{work_id}/game-profile")
def game_profile_edit(
    work_id: int,
    request: Request,
    confidence: str | None = Form(None),
    completion: str | None = Form(None),
    playtime_minutes: int | None = Form(None),
    liked_aspects: str = Form(""),
    disliked_aspects: str = Form(""),
    notes_private: str = Form(""),
) -> RedirectResponse:
    patch = GameProfilePatch(
        confidence=confidence or None,
        completion=completion or None,
        playtime_minutes=playtime_minutes,
        liked_aspects=tuple(item.strip() for item in liked_aspects.split(",") if item.strip()),
        disliked_aspects=tuple(
            item.strip() for item in disliked_aspects.split(",") if item.strip()
        ),
        notes_private=notes_private,
    )
    try:
        with request.app.state.session_factory.begin() as session:
            edit_game_profile(session, work_id, patch)
    except GameProfileError as exc:
        raise HTTPException(422, str(exc)) from None
    return RedirectResponse(f"/works/{work_id}", status_code=303)

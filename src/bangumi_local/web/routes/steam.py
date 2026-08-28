from __future__ import annotations

from dataclasses import dataclass
import json
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from bangumi_local.adapters.steam import SteamDataError, detect_steam, read_steam_snapshot
from bangumi_local.config import Settings
from bangumi_local.db.models import (
    LibraryEntry,
    LibraryMatchCandidate,
    MediaBinding,
    MediaSource,
)
from bangumi_local.domain.plans import stable_json
from bangumi_local.domain.steam import (
    SteamConfigurationError,
    load_steam_rules,
    steam_rule_configuration_from_payload,
)
from bangumi_local.services.plans import PlanError
from bangumi_local.services.read_models import SteamSummary, steam_summary
from bangumi_local.services.steam_import import SteamImportSummary, preview_steam_import
from bangumi_local.services.steam_library import (
    SteamCollectionListItem,
    SteamEntryListItem,
    SteamLibraryError,
    list_steam_collections,
    list_steam_entries,
    steam_account,
)
from bangumi_local.services.steam_match_plans import revise_steam_match_plan
from bangumi_local.services.steam_matching import SteamMatchError, set_match_disposition
from bangumi_local.services.steam_titles import (
    SteamTitleError,
    clear_manual_title,
    set_manual_title,
)
from bangumi_local.web.dependencies import get_session, get_settings


router = APIRouter(prefix="/steam")


@dataclass(frozen=True, slots=True)
class SteamDetectionView:
    root_detected: bool
    account_count: int
    account_selected: bool
    category_source: str
    category_file_available: bool
    legacy_file_available: bool
    local_config_available: bool
    installed_manifest_count: int


@dataclass(frozen=True, slots=True)
class MatchCandidateCard:
    subject_id: int
    title: str
    title_original: str
    url: str
    release_date: str | None
    rank: int
    score: int
    reasons: tuple[str, ...]
    aliases: tuple[str, ...]
    cover_src: str
    margin_from_next: int | None
    review_mode: str
    summary: str | None
    public_tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatchWorkbenchView:
    app_id: str
    title: str
    match_status: str
    work_id: int | None
    collections: tuple[str, ...]
    installed: bool | None
    playtime_minutes: int | None
    cover_src: str
    candidates: tuple[MatchCandidateCard, ...]


@dataclass(frozen=True, slots=True)
class JobRequestView:
    job_id: str
    kind: str
    parameters: tuple[tuple[str, str], ...]
    requires_network: bool
    remote_writes: bool = False

    @property
    def canonical_parameters(self) -> str:
        return stable_json(dict(self.parameters))


def _template(
    request: Request,
    name: str,
    context: dict[str, object],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=name,
        context=context,
        status_code=status_code,
    )


def _job(
    request: Request,
    *,
    kind: str,
    parameters: dict[str, object],
    requires_network: bool = True,
) -> HTMLResponse:
    values = tuple(
        (key, stable_json(value) if isinstance(value, (list, dict)) else str(value))
        for key, value in sorted(parameters.items())
    )
    from bangumi_local.services.jobs import enqueue_job

    with request.app.state.session_factory.begin() as session:
        stored = enqueue_job(
            session,
            kind=kind,
            capability="remote_read" if requires_network else "local_write",
            config=parameters,
        )
        job_id = stored.id
    job = JobRequestView(
        job_id=job_id,
        kind=kind,
        parameters=values,
        requires_network=requires_network,
    )
    return _template(
        request,
        "steam/job.html",
        {
            "job": job,
            "page_title": "Steam 后台任务请求",
        },
        status_code=202,
    )


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _match_workbench(
    session: Session,
    settings: Settings,
    app_id: str,
) -> MatchWorkbenchView:
    try:
        account = steam_account(session, settings.steam_account_id)
    except SteamLibraryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    entry = session.scalar(
        select(LibraryEntry).where(
            LibraryEntry.source_account_id == account.id,
            LibraryEntry.external_id == app_id,
        )
    )
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Steam AppID {app_id} was not found.")
    rows = tuple(
        session.scalars(
            select(LibraryMatchCandidate)
            .where(LibraryMatchCandidate.library_entry_id == entry.id)
            .order_by(LibraryMatchCandidate.rank, LibraryMatchCandidate.subject_id)
        ).all()
    )
    cards: list[MatchCandidateCard] = []
    for index, row in enumerate(rows):
        try:
            snapshot = json.loads(row.snapshot_json)
            reasons = tuple(json.loads(row.reasons_json))
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(status_code=409, detail="Stored Steam candidate is invalid.") from exc
        next_score = rows[index + 1].score if index + 1 < len(rows) else None
        margin = row.score - next_score if next_score is not None else row.score
        exact = "normalized_title_exact" in reasons
        risk_free = not any(reason.startswith("penalty_") for reason in reasons)
        automatic = index == 0 and row.score >= 95 and margin >= 20 and exact and risk_free
        candidate_digest = session.scalar(
            select(MediaSource.current_blob_sha256)
            .where(
                MediaSource.provider == "bangumi",
                MediaSource.external_id == str(row.subject_id),
                MediaSource.current_blob_sha256.is_not(None),
            )
            .order_by(MediaSource.variant)
            .limit(1)
        )
        cards.append(
            MatchCandidateCard(
                subject_id=row.subject_id,
                title=str(
                    snapshot.get("title")
                    or snapshot.get("title_cn")
                    or snapshot.get("title_original")
                    or row.subject_id
                ),
                title_original=str(snapshot.get("title_original") or ""),
                # Never trust a persisted external URL as an HTML navigation target.
                url=f"https://bgm.tv/subject/{row.subject_id}",
                release_date=(str(snapshot["release_date"]) if snapshot.get("release_date") else None),
                rank=row.rank,
                score=row.score,
                reasons=reasons,
                aliases=tuple(str(item) for item in snapshot.get("aliases", ())),
                cover_src=(
                    f"/media/blob/{candidate_digest}"
                    if candidate_digest
                    else "/media/placeholder"
                ),
                margin_from_next=margin,
                review_mode="automatic_candidate" if automatic else "manual_review",
                summary=(str(snapshot["summary"]) if snapshot.get("summary") else None),
                public_tags=tuple(str(item) for item in snapshot.get("public_tags", ())),
            )
        )
    entry_view = next(
        (
            item
            for item in list_steam_entries(session, account_id=settings.steam_account_id)
            if item.app_id == app_id
        ),
        None,
    )
    names = entry_view.collections if entry_view is not None else ()
    digest = session.scalar(
        select(MediaSource.current_blob_sha256)
        .join(MediaBinding, MediaBinding.media_source_id == MediaSource.id)
        .where(
            MediaBinding.library_entry_id == entry.id,
            MediaBinding.role == "cover",
            MediaSource.current_blob_sha256.is_not(None),
        )
        .order_by(MediaBinding.priority.desc())
        .limit(1)
    )
    return MatchWorkbenchView(
        app_id=entry.external_id,
        title=entry.title_observed or f"Steam App {entry.external_id}",
        match_status=entry.match_status,
        work_id=entry.work_id,
        collections=names,
        installed=entry.installed,
        playtime_minutes=entry.playtime_minutes,
        cover_src=f"/media/blob/{digest}" if digest else "/media/placeholder",
        candidates=tuple(cards),
    )


@router.get("", response_class=HTMLResponse)
def steam_home(
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    summary: SteamSummary = steam_summary(session)
    return _template(
        request,
        "steam/index.html",
        {"summary": summary, "page_title": "Steam 工作台"},
    )


@router.get("/detect", response_class=HTMLResponse)
def steam_detect(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    try:
        detected = detect_steam(settings)
    except SteamDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    view = SteamDetectionView(
        root_detected=True,
        account_count=len(detected.account_ids),
        account_selected=bool(detected.selected_account_id),
        category_source=detected.category_source,
        category_file_available=detected.category_file_available,
        legacy_file_available=detected.legacy_file_available,
        local_config_available=detected.local_config_available,
        installed_manifest_count=detected.installed_manifest_count,
    )
    return _template(
        request,
        "steam/detect.html",
        {"detection": view, "page_title": "Steam 本地检测"},
    )


@router.get("/import", response_class=HTMLResponse)
def steam_import_form(request: Request) -> HTMLResponse:
    return _template(request, "steam/import.html", {"page_title": "Steam 导入预览"})


@router.post("/import/preview", response_class=HTMLResponse)
def steam_import_preview(
    request: Request,
    allow_network: bool = Form(False),
    apply_local: bool = Form(False),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    if allow_network or apply_local:
        return _job(
            request,
            kind="steam_import_apply" if apply_local else "steam_import_preview",
            parameters={"allow_network": allow_network, "apply_local": apply_local},
            requires_network=allow_network,
        )
    try:
        snapshot = read_steam_snapshot(settings, allow_network=False)
        factory = request.app.state.session_factory
        with factory() as session:
            summary: SteamImportSummary = preview_steam_import(session, snapshot)
            session.rollback()
    except (SteamDataError, SteamLibraryError) as exc:
        raise _bad_request(exc) from None
    return _template(
        request,
        "steam/import_result.html",
        {"summary": summary, "page_title": "Steam 导入预览结果"},
    )


@router.get("/collections", response_class=HTMLResponse)
def steam_collections(
    request: Request,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    try:
        items: list[SteamCollectionListItem] = list_steam_collections(
            session, settings.steam_account_id
        )
    except SteamLibraryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return _template(
        request,
        "steam/collections.html",
        {"items": tuple(items), "page_title": "Steam 分类"},
    )


@router.get("/titles", response_class=HTMLResponse)
def steam_titles(
    request: Request,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    try:
        items = list_steam_entries(session, account_id=settings.steam_account_id)
    except SteamLibraryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    missing = tuple(item for item in items if not (item.title or "").strip())
    return _template(
        request,
        "steam/titles.html",
        {
            "missing": missing,
            "missing_count": len(missing),
            "page_title": "Steam 标题补全",
        },
    )


@router.post("/jobs/titles-complete", response_class=HTMLResponse)
def steam_titles_complete_job(
    request: Request,
    selector_mode: str | None = Form(None),
    appids: str | None = Form(None),
    all_missing: bool = Form(False),
    max_items: int = Form(250, ge=1, le=250),
    request_delay_ms: int = Form(250, ge=0),
    allow_network: bool = Form(False),
) -> HTMLResponse:
    if selector_mode is not None:
        if selector_mode not in {"all_missing", "appids"}:
            raise HTTPException(status_code=400, detail="Unsupported Steam title selector.")
        all_missing = selector_mode == "all_missing"
        appids = appids if selector_mode == "appids" else None
    if bool(appids) == all_missing:
        raise HTTPException(status_code=400, detail="Choose AppIDs or all missing titles.")
    if appids and any(not value.strip().isdigit() for value in appids.split(",")):
        raise HTTPException(status_code=400, detail="Steam AppIDs must be numeric.")
    if not allow_network:
        raise HTTPException(status_code=400, detail="Steam title completion requires network permission.")
    return _job(
        request,
        kind="steam_titles_complete",
        parameters={
            "appids": appids or "",
            "all_missing": all_missing,
            "max_items": max_items,
            "request_delay_ms": request_delay_ms,
            "allow_network": True,
        },
    )


@router.post("/titles/{app_id}/manual")
def steam_title_manual(
    app_id: str,
    request: Request,
    title: str = Form(..., min_length=1, max_length=500),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    try:
        with request.app.state.session_factory.begin() as session:
            set_manual_title(
                session,
                account_id=settings.steam_account_id,
                app_id=app_id,
                title=title,
            )
    except (SteamTitleError, SteamLibraryError) as exc:
        raise _bad_request(exc) from None
    return RedirectResponse(f"/steam/match/{app_id}", status_code=303)


@router.post("/titles/{app_id}/clear")
def steam_title_clear(
    app_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    try:
        with request.app.state.session_factory.begin() as session:
            clear_manual_title(
                session, account_id=settings.steam_account_id, app_id=app_id
            )
    except (SteamTitleError, SteamLibraryError) as exc:
        raise _bad_request(exc) from None
    return RedirectResponse(f"/steam/match/{app_id}", status_code=303)


def _library_page(
    request: Request,
    session: Session,
    settings: Settings,
    *,
    collection: str | None,
    collection_regex: str | None,
    match_status: str | None,
    unmatched_only: bool,
) -> HTMLResponse:
    if collection and collection_regex:
        raise HTTPException(status_code=400, detail="Choose collection or collection_regex, not both.")
    try:
        items = list_steam_entries(
            session,
            account_id=settings.steam_account_id,
            collection_name=collection or None,
            collection_regex=collection_regex or None,
            match_status=match_status or None,
        )
    except (SteamLibraryError, ValueError) as exc:
        raise _bad_request(exc) from None
    if unmatched_only:
        items = [item for item in items if item.match_status != "confirmed"]
    account = steam_account(session, settings.steam_account_id)
    cards = []
    for item in items:
        entry = session.scalar(
            select(LibraryEntry).where(
                LibraryEntry.source_account_id == account.id,
                LibraryEntry.external_id == item.app_id,
            )
        )
        digest = (
            session.scalar(
                select(MediaSource.current_blob_sha256)
                .join(MediaBinding, MediaBinding.media_source_id == MediaSource.id)
                .where(
                    MediaBinding.library_entry_id == entry.id,
                    MediaBinding.role == "cover",
                    MediaSource.current_blob_sha256.is_not(None),
                )
                .order_by(MediaBinding.priority.desc())
                .limit(1)
            )
            if entry is not None
            else None
        )
        cards.append({"row": item, "cover_src": f"/media/blob/{digest}" if digest else "/media/placeholder"})
    return _template(
        request,
        "steam/library.html",
        {
            "items": tuple(cards),
            "collection": collection or "",
            "collection_regex": collection_regex or "",
            "match_status": match_status or "",
            "unmatched_only": unmatched_only,
            "page_title": "Steam 未匹配" if unmatched_only else "Steam 库",
        },
    )


@router.get("/library", response_class=HTMLResponse)
def steam_library(
    request: Request,
    collection: str | None = Query(None, max_length=200),
    collection_regex: str | None = Query(None, max_length=200),
    match_status: str | None = Query(None, max_length=32),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    return _library_page(
        request, session, settings,
        collection=collection, collection_regex=collection_regex,
        match_status=match_status, unmatched_only=False,
    )


@router.get("/unmatched", response_class=HTMLResponse)
def steam_unmatched(
    request: Request,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    return _library_page(
        request, session, settings,
        collection=None, collection_regex=None, match_status=None, unmatched_only=True,
    )


@router.get("/match/plan", response_class=HTMLResponse)
def match_plan_form(request: Request) -> HTMLResponse:
    return _template(request, "steam/match_plan.html", {"page_title": "Steam 批量匹配计划"})


@router.post("/jobs/match-plan", response_class=HTMLResponse)
def match_plan_job(
    request: Request,
    selector_mode: str | None = Form(None),
    appids: str | None = Form(None),
    collection: str | None = Form(None),
    collection_regex: str | None = Form(None),
    all_unmatched: bool = Form(False),
    auto_threshold: int = Form(95, ge=0, le=100),
    min_margin: int = Form(20, ge=0, le=100),
    allow_nonexact_auto: bool = Form(False),
    limit: int = Form(10, ge=1, le=50),
    offset: int = Form(0, ge=0),
    max_items: int = Form(250, ge=1, le=250),
    include_no_subject: bool = Form(False),
    include_deferred: bool = Form(False),
    candidate_image_policy: str = Form("metadata"),
    request_delay_ms: int = Form(250, ge=0),
    allow_network: bool = Form(False),
) -> HTMLResponse:
    if selector_mode is not None:
        if selector_mode not in {"appids", "collection", "collection_regex", "all_unmatched"}:
            raise HTTPException(status_code=400, detail="Unsupported Steam match selector.")
        all_unmatched = selector_mode == "all_unmatched"
        appids = appids if selector_mode == "appids" else None
        collection = collection if selector_mode == "collection" else None
        collection_regex = collection_regex if selector_mode == "collection_regex" else None
    selectors = [bool(appids), bool(collection), bool(collection_regex), all_unmatched]
    if sum(selectors) != 1:
        raise HTTPException(status_code=400, detail="Choose exactly one batch selector.")
    if not allow_network:
        raise HTTPException(status_code=400, detail="Batch matching requires explicit network permission.")
    if candidate_image_policy not in {"none", "metadata", "cache"}:
        raise HTTPException(status_code=400, detail="Unsupported candidate image policy.")
    return _job(
        request,
        kind="steam_match_plan",
        parameters={
            "appids": appids or "", "collection": collection or "",
            "collection_regex": collection_regex or "", "all_unmatched": all_unmatched,
            "auto_threshold": auto_threshold, "min_margin": min_margin,
            "allow_nonexact_auto": allow_nonexact_auto, "limit": limit,
            "offset": offset, "max_items": max_items,
            "include_no_subject": include_no_subject,
            "include_deferred": include_deferred,
            "candidate_image_policy": candidate_image_policy,
            "request_delay_ms": request_delay_ms, "allow_network": True,
        },
    )


@router.get("/status-plan", response_class=HTMLResponse)
def status_plan_form(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    try:
        configuration = load_steam_rules(settings.steam_config)
    except SteamConfigurationError as exc:
        raise HTTPException(422, str(exc)) from None
    rules = tuple(
        {
            "match": rule.match,
            "pattern": rule.pattern,
            "status": rule.status.label,
            "case_sensitive": rule.case_sensitive,
        }
        for rule in configuration.rules
    )
    return _template(
        request,
        "steam/status_plan.html",
        {
            "page_title": "Steam 状态计划",
            "saved_rules": rules,
            "saved_remaining_status": (
                configuration.remaining_status.label
                if configuration.remaining_status is not None
                else ""
            ),
        },
    )


@router.post("/jobs/status-plan", response_class=HTMLResponse)
def status_plan_job(
    request: Request,
    appids: str | None = Form(None),
    all_eligible: bool = Form(False),
    remaining_status: str | None = Form(None),
    remaining_policy: str | None = Form(None),
    rule_mode: str = Form("saved"),
    rules_json: str = Form(""),
    add_tag: str | None = Form(None),
) -> HTMLResponse:
    if bool(appids) == all_eligible:
        raise HTTPException(status_code=400, detail="Choose appids or all_eligible.")
    allowed_statuses = {"", "default", "local", "wish", "done", "doing", "on-hold", "dropped"}
    selected_remaining = remaining_policy if remaining_policy is not None else (remaining_status or "")
    if selected_remaining not in allowed_statuses:
        raise HTTPException(status_code=400, detail="Unsupported remaining status.")
    if rule_mode not in {"saved", "custom"}:
        raise HTTPException(status_code=400, detail="Unsupported Steam rule mode.")
    custom_rules: list[object] | None = None
    if rule_mode == "custom":
        try:
            loaded_rules = json.loads(rules_json)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Steam rules JSON is invalid.") from None
        custom_rules = loaded_rules if isinstance(loaded_rules, list) else None
        if custom_rules is None:
            raise HTTPException(status_code=400, detail="Steam rules must be a list.")
        try:
            steam_rule_configuration_from_payload(
                {
                    "rules": custom_rules,
                    "remaining_status": None,
                    "allow_network": False,
                }
            )
        except SteamConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return _job(
        request,
        kind="steam_status_plan",
        parameters={
            "appids": appids or "", "all_eligible": all_eligible,
            "remaining_policy": selected_remaining or "default",
            "rule_mode": rule_mode,
            "rules": custom_rules or [],
            "add_tag": add_tag or "",
        },
    )


@router.post("/jobs/match-search", response_class=HTMLResponse)
def match_search_job(
    request: Request,
    app_id: str = Form(..., min_length=1, max_length=20),
    query: str | None = Form(None, max_length=300),
    limit: int = Form(10, ge=1, le=50),
    allow_network: bool = Form(False),
) -> HTMLResponse:
    if not app_id.isdigit():
        raise HTTPException(status_code=400, detail="Steam AppID must be numeric.")
    if not allow_network:
        raise HTTPException(status_code=400, detail="Candidate search requires network permission.")
    return _job(
        request,
        kind="steam_match_search",
        parameters={"app_id": app_id, "query": query or "", "limit": limit, "allow_network": True},
    )


@router.post("/match/plan/{plan_id}/revise")
def match_revise(
    plan_id: str,
    request: Request,
    app_id: str = Form(..., min_length=1, max_length=20),
    subject_id: int | None = Form(None, ge=1),
    manual_review: bool = Form(False),
    no_subject: bool = Form(False),
    defer: bool = Form(False),
    allow_network: bool = Form(False),
) -> Response:
    if not app_id.isdigit():
        raise HTTPException(status_code=400, detail="Steam AppID must be numeric.")
    if sum((subject_id is not None, manual_review, no_subject, defer)) != 1:
        raise HTTPException(status_code=400, detail="Choose exactly one revision decision.")
    if subject_id is not None:
        if not allow_network:
            raise HTTPException(status_code=400, detail="Subject validation requires network permission.")
        return _job(
            request,
            kind="steam_match_revise_subject",
            parameters={
                "plan_id": plan_id, "app_id": app_id,
                "subject_id": subject_id, "allow_network": True,
            },
        )
    decision = "manual_review" if manual_review else "no_subject" if no_subject else "deferred"
    factory = request.app.state.session_factory
    try:
        with factory.begin() as session:
            stored = revise_steam_match_plan(
                session, None, plan_id=plan_id, app_id=app_id, decision=decision
            )
    except (PlanError, SteamMatchError, SteamLibraryError) as exc:
        raise _bad_request(exc) from None
    return RedirectResponse(f"/plans/{stored.plan.id}", status_code=303)


@router.post("/match/revise")
def match_revise_form(
    request: Request,
    plan_id: str = Form(..., min_length=36, max_length=36),
    app_id: str = Form(..., min_length=1, max_length=20),
    subject_id: int | None = Form(None, ge=1),
    manual_review: bool = Form(False),
    no_subject: bool = Form(False),
    defer: bool = Form(False),
    allow_network: bool = Form(False),
) -> Response:
    return match_revise(
        plan_id,
        request,
        app_id,
        subject_id,
        manual_review,
        no_subject,
        defer,
        allow_network,
    )


@router.post("/jobs/match-confirm", response_class=HTMLResponse)
def match_confirm_job(
    request: Request,
    app_id: str = Form(..., min_length=1, max_length=20),
    subject_id: int = Form(..., ge=1),
    allow_network: bool = Form(False),
) -> HTMLResponse:
    if not app_id.isdigit():
        raise HTTPException(status_code=400, detail="Steam AppID must be numeric.")
    if not allow_network:
        raise HTTPException(status_code=400, detail="Subject validation requires network permission.")
    return _job(
        request,
        kind="steam_match_confirm",
        parameters={"app_id": app_id, "subject_id": subject_id, "allow_network": True},
    )


def _match_disposition(
    request: Request,
    settings: Settings,
    app_id: str,
    decision: str,
    reason: str | None,
) -> RedirectResponse:
    if not app_id.isdigit():
        raise HTTPException(status_code=400, detail="Steam AppID must be numeric.")
    factory = request.app.state.session_factory
    try:
        with factory.begin() as session:
            set_match_disposition(
                session,
                app_id=app_id,
                account_id=settings.steam_account_id,
                decision=decision,
                reason=(reason or "").strip() or None,
            )
    except (SteamMatchError, SteamLibraryError) as exc:
        raise _bad_request(exc) from None
    return RedirectResponse(f"/steam/match/{app_id}", status_code=303)


@router.post("/match/{app_id}/no-subject")
def match_no_subject(
    app_id: str,
    request: Request,
    reason: str | None = Form(None, max_length=1000),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    return _match_disposition(request, settings, app_id, "no_subject", reason)


@router.post("/match/{app_id}/defer")
def match_defer(
    app_id: str,
    request: Request,
    reason: str | None = Form(None, max_length=1000),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    return _match_disposition(request, settings, app_id, "deferred", reason)


@router.post("/match/{app_id}/reopen")
def match_reopen(
    app_id: str,
    request: Request,
    reason: str | None = Form(None, max_length=1000),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    return _match_disposition(request, settings, app_id, "reopened", reason)


@router.get("/match/{app_id}", response_class=HTMLResponse)
def match_show(
    app_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    if not app_id.isdigit():
        raise HTTPException(status_code=400, detail="Steam AppID must be numeric.")
    view = _match_workbench(session, settings, app_id)
    return _template(
        request,
        "steam/match.html",
        {"item": view, "page_title": f"Steam 匹配 {app_id}"},
    )

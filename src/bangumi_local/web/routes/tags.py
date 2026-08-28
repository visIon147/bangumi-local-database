from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from bangumi_local.domain.tags import (
    DEFAULT_GALGAME_CLASSIFICATION_TAG,
    DEFAULT_GAME_CLASSIFICATION_TAG,
    validate_tag,
)
from bangumi_local.domain.models import SubjectType
from bangumi_local.web.action_protocol import invalid_action, job_required
from bangumi_local.services.jobs import enqueue_job


router = APIRouter(prefix="/tags")


def _validate_selector(mode: str, ids: str, public_tag: str) -> str | None:
    if mode not in {"ids", "all_current", "public_tag"}:
        return "Tag 选择器无效。"
    if mode == "ids":
        try:
            values = tuple(int(value.strip()) for value in ids.split(",") if value.strip())
        except ValueError:
            return "Subject ID 必须是逗号分隔的正整数。"
        if not values or any(value < 1 for value in values):
            return "至少提供一个正整数 Subject ID。"
    if mode == "public_tag":
        try:
            validate_tag(public_tag)
        except ValueError as exc:
            return str(exc)
    return None


@router.get("", response_class=HTMLResponse)
def tags_home(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="tags/index.html",
        context={
            "page_title": "批量 Tag",
            "galgame_tag": DEFAULT_GALGAME_CLASSIFICATION_TAG,
            "game_tag": DEFAULT_GAME_CLASSIFICATION_TAG,
            "subject_types": tuple((value.kind, value.kind) for value in SubjectType),
        },
    )


@router.post("/bulk")
def bulk_tag_action(
    request: Request,
    operation: str = Form(...),
    tag: str = Form(""),
    old_tag: str = Form(""),
    new_tag: str = Form(""),
    selector_mode: str = Form(...),
    subject_ids: str = Form(""),
    public_tag: str = Form(""),
    subject_type: str = Form(""),
) -> HTMLResponse:
    if operation not in {"add", "remove", "rename"}:
        return invalid_action(request, "Tag 操作无效。")
    try:
        if operation in {"add", "remove"}:
            validate_tag(tag)
        else:
            validated_old = validate_tag(old_tag)
            validated_new = validate_tag(new_tag)
            if validated_old == validated_new:
                return invalid_action(request, "新旧 Tag 必须不同。")
    except ValueError as exc:
        return invalid_action(request, str(exc))
    selector_error = _validate_selector(selector_mode, subject_ids, public_tag)
    if selector_error:
        return invalid_action(request, selector_error)
    selector: dict[str, object] = {"mode": selector_mode}
    try:
        parsed_type = SubjectType.parse(subject_type) if subject_type else None
    except ValueError as exc:
        return invalid_action(request, str(exc))
    if parsed_type is not None:
        selector["subject_type"] = int(parsed_type)
    if selector_mode == "ids":
        selector["ids"] = list(
            dict.fromkeys(int(value.strip()) for value in subject_ids.split(",") if value.strip())
        )
    elif selector_mode == "public_tag":
        selector["public_tag"] = public_tag
    config = {
        "operation": operation,
        "tag": tag if operation in {"add", "remove"} else None,
        "old_tag": old_tag if operation == "rename" else None,
        "new_tag": new_tag if operation == "rename" else None,
        "selector": selector,
    }
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="bulk_tag_plan",
            capability="remote_read",
            config=config,
        )
        job_id = job.id
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/classify-games")
def classify_games_action(
    request: Request,
    public_tag: str = Form("Galgame"),
    galgame_tag: str = Form(DEFAULT_GALGAME_CLASSIFICATION_TAG),
    game_tag: str = Form(DEFAULT_GAME_CLASSIFICATION_TAG),
) -> HTMLResponse:
    try:
        public_tag = validate_tag(public_tag)
        galgame_tag = validate_tag(galgame_tag)
        game_tag = validate_tag(game_tag)
    except ValueError as exc:
        return invalid_action(request, str(exc))
    if galgame_tag == game_tag:
        return invalid_action(request, "Galgame 与普通游戏分类 Tag 必须不同。")
    with request.app.state.session_factory.begin() as session:
        job = enqueue_job(
            session,
            kind="classify_games_plan",
            capability="remote_read",
            config={
                "public_tag": public_tag,
                "galgame_tag": galgame_tag,
                "game_tag": game_tag,
            },
        )
        job_id = job.id
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)

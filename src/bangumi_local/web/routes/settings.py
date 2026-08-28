from __future__ import annotations

import json
import math
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from bangumi_local import __version__
from bangumi_local.domain.steam import SteamConfigurationError, load_steam_rules
from bangumi_local.services.configuration import (
    SettingsEditError,
    environment_managed,
    save_steam_rule_configuration,
    update_dotenv,
)
from bangumi_local.services.jobs import enqueue_job
from bangumi_local.web.action_protocol import invalid_action
from bangumi_local.web.dependencies import get_session, get_write_session


router = APIRouter(prefix="/settings")
WEB_BASE_URLS = ("https://bgm.tv", "https://bangumi.tv", "https://chii.in")
ENV_ALIASES = {
    "bangumi_token": ("BANGUMI_ACCESS_TOKEN",),
    "bangumi_username": ("BANGUMI_USERNAME",),
    "bangumi_user_agent": ("BANGUMI_USER_AGENT",),
    "bangumi_web_base": ("BLD_BANGUMI_WEB_BASE_URL", "BANGUMI_WEB_BASE_URL"),
    "steam_root": ("BLD_STEAM_ROOT", "STEAM_ROOT"),
    "steam_account_id": ("BLD_STEAM_ACCOUNT_ID", "STEAM_ACCOUNT_ID"),
    "steam_id64": ("BLD_STEAM_ID64", "STEAM_ID64"),
    "steam_api_key": ("STEAM_WEB_API_KEY", "BLD_STEAM_WEB_API_KEY"),
    "request_timeout": ("BANGUMI_REQUEST_TIMEOUT_SECONDS",),
    "write_delay": ("BANGUMI_WRITE_DELAY_MS",),
    "max_retries": ("BANGUMI_MAX_RETRIES",),
    "retry_base": ("BANGUMI_RETRY_BASE_SECONDS",),
    "image_policy": ("BLD_IMAGE_POLICY", "BGV_IMAGE_POLICY"),
    "image_item_limit": ("BLD_IMAGE_MAX_ITEM_BYTES", "BGV_IMAGE_MAX_ITEM_BYTES"),
    "image_cache_limit": ("BLD_IMAGE_CACHE_MAX_BYTES", "BGV_IMAGE_CACHE_MAX_BYTES"),
}


def _managed_view() -> dict[str, bool]:
    return {
        name: environment_managed(*aliases) for name, aliases in ENV_ALIASES.items()
    }


def _env_path(request: Request) -> Path:
    path = request.app.state.settings_env_file
    if path is None:
        raise SettingsEditError(
            "This application instance has no editable environment file."
        )
    return Path(path)


def _allowed_update(
    updates: dict[str, str | None],
    field: str,
    key: str,
    value: str | None,
) -> None:
    if not environment_managed(*ENV_ALIASES[field]):
        updates[key] = value


def _restart_redirect(section: str) -> RedirectResponse:
    return RedirectResponse(f"/settings?saved={section}&restart=1", status_code=303)


@router.get("", response_class=HTMLResponse)
def settings_page(
    request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    settings = request.app.state.settings
    assert session.bind is not None
    revision = (
        session.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
        if "alembic_version" in inspect(session.bind).get_table_names()
        else None
    )
    rules_error: str | None = None
    try:
        steam_rules = load_steam_rules(settings.steam_config)
    except SteamConfigurationError:
        steam_rules = None
        rules_error = "Steam 规则配置无效；请先检查本地 steam.toml。"
    view = {
        "version": __version__,
        "schema_revision": revision or "unknown",
        "editing_enabled": request.app.state.settings_env_file is not None,
        "saved_section": request.query_params.get("saved"),
        "restart_required": request.query_params.get("restart") == "1",
        "managed": _managed_view(),
        "bangumi_configured": all(
            (
                settings.bangumi_access_token is not None,
                bool(settings.bangumi_username),
                bool(settings.bangumi_user_agent),
            )
        ),
        "bangumi_token_configured": settings.bangumi_access_token is not None,
        "bangumi_username": settings.bangumi_username or "",
        "bangumi_user_agent": settings.bangumi_user_agent or "",
        "bangumi_web_base_url": settings.bangumi_web_base_url,
        "bangumi_web_base_urls": WEB_BASE_URLS,
        "bangumi_api_official": settings.bangumi_base_url.rstrip("/") == "https://api.bgm.tv",
        "steam_local_configured": settings.steam_root is not None,
        "steam_web_configured": all(
            (settings.steam_id64, settings.steam_web_api_key is not None)
        ),
        "steam_api_key_configured": settings.steam_web_api_key is not None,
        "steam_root": str(settings.steam_root) if settings.steam_root is not None else "",
        "steam_account_id": settings.steam_account_id or "",
        "steam_id64": settings.steam_id64 or "",
        "request_timeout": settings.bangumi_request_timeout_seconds,
        "image_policy": settings.image_policy,
        "image_item_limit": settings.image_max_item_bytes,
        "image_cache_limit": settings.image_cache_max_bytes,
        "write_delay_ms": settings.bangumi_write_delay_ms,
        "max_retries": settings.bangumi_max_retries,
        "retry_base_seconds": settings.bangumi_retry_base_seconds,
        "steam_rules": steam_rules,
        "steam_rules_error": rules_error,
    }
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"settings_view": view, "page_title": "设置与诊断"},
    )


@router.post("/bangumi")
def save_bangumi_settings(
    request: Request,
    access_token: str = Form(""),
    clear_access_token: bool = Form(False),
    username: str = Form(""),
    user_agent: str = Form(""),
    web_base_url: str = Form("https://bgm.tv"),
) -> HTMLResponse:
    if web_base_url not in WEB_BASE_URLS:
        return invalid_action(request, "Bangumi 网页域名不在允许列表中。")
    updates: dict[str, str | None] = {}
    if clear_access_token:
        _allowed_update(updates, "bangumi_token", "BANGUMI_ACCESS_TOKEN", None)
    elif access_token:
        _allowed_update(
            updates, "bangumi_token", "BANGUMI_ACCESS_TOKEN", access_token
        )
    _allowed_update(
        updates,
        "bangumi_username",
        "BANGUMI_USERNAME",
        username.strip() or None,
    )
    _allowed_update(
        updates,
        "bangumi_user_agent",
        "BANGUMI_USER_AGENT",
        user_agent.strip() or None,
    )
    _allowed_update(
        updates,
        "bangumi_web_base",
        "BLD_BANGUMI_WEB_BASE_URL",
        web_base_url,
    )
    try:
        update_dotenv(_env_path(request), updates)
    except SettingsEditError as exc:
        return invalid_action(request, str(exc))
    return _restart_redirect("bangumi")


@router.post("/steam")
def save_steam_settings(
    request: Request,
    steam_root: str = Form(""),
    account_id: str = Form(""),
    steam_id64: str = Form(""),
    web_api_key: str = Form(""),
    clear_web_api_key: bool = Form(False),
) -> HTMLResponse:
    normalized_account = account_id.strip()
    normalized_id64 = steam_id64.strip()
    if normalized_account and not normalized_account.isdigit():
        return invalid_action(request, "Steam userdata account ID 必须是数字。")
    if normalized_id64 and not normalized_id64.isdigit():
        return invalid_action(request, "Steam ID64 必须是数字。")
    updates: dict[str, str | None] = {}
    _allowed_update(
        updates, "steam_root", "BLD_STEAM_ROOT", steam_root.strip() or None
    )
    _allowed_update(
        updates,
        "steam_account_id",
        "BLD_STEAM_ACCOUNT_ID",
        normalized_account or None,
    )
    _allowed_update(
        updates, "steam_id64", "BLD_STEAM_ID64", normalized_id64 or None
    )
    if clear_web_api_key:
        _allowed_update(updates, "steam_api_key", "STEAM_WEB_API_KEY", None)
    elif web_api_key:
        _allowed_update(
            updates, "steam_api_key", "STEAM_WEB_API_KEY", web_api_key
        )
    try:
        update_dotenv(_env_path(request), updates)
    except SettingsEditError as exc:
        return invalid_action(request, str(exc))
    return _restart_redirect("steam")


@router.post("/safety")
def save_safety_settings(
    request: Request,
    request_timeout: float | None = Form(None),
    write_delay_ms: int | None = Form(None),
    max_retries: int | None = Form(None),
    retry_base_seconds: float | None = Form(None),
    image_policy: str | None = Form(None),
    image_item_limit: int | None = Form(None),
    image_cache_limit: int | None = Form(None),
) -> HTMLResponse:
    if request_timeout is not None and (
        not math.isfinite(request_timeout) or request_timeout <= 0
    ):
        return invalid_action(request, "请求超时必须是正数。")
    if (write_delay_ms is not None and write_delay_ms < 0) or (
        max_retries is not None and not 0 <= max_retries <= 10
    ):
        return invalid_action(request, "写入间隔或最大重试超出允许范围。")
    if retry_base_seconds is not None and (
        not math.isfinite(retry_base_seconds) or retry_base_seconds <= 0
    ):
        return invalid_action(request, "退避基数必须是正数。")
    if image_policy is not None and image_policy not in {"none", "metadata", "cache"}:
        return invalid_action(request, "图片策略无效。")
    if (image_item_limit is not None and image_item_limit < 1024) or (
        image_cache_limit is not None and image_cache_limit < 1024
    ):
        return invalid_action(request, "图片大小限制不得低于 1024 bytes。")
    updates: dict[str, str | None] = {}
    values = (
        ("request_timeout", "BANGUMI_REQUEST_TIMEOUT_SECONDS", request_timeout),
        ("write_delay", "BANGUMI_WRITE_DELAY_MS", write_delay_ms),
        ("max_retries", "BANGUMI_MAX_RETRIES", max_retries),
        ("retry_base", "BANGUMI_RETRY_BASE_SECONDS", retry_base_seconds),
        ("image_policy", "BLD_IMAGE_POLICY", image_policy),
        ("image_item_limit", "BLD_IMAGE_MAX_ITEM_BYTES", image_item_limit),
        ("image_cache_limit", "BLD_IMAGE_CACHE_MAX_BYTES", image_cache_limit),
    )
    for field, key, value in values:
        if value is not None:
            _allowed_update(updates, field, key, str(value))
    try:
        update_dotenv(_env_path(request), updates)
    except SettingsEditError as exc:
        return invalid_action(request, str(exc))
    return _restart_redirect("safety")


@router.post("/steam-rules")
def save_steam_rules(
    request: Request,
    rules_json: str = Form(...),
    remaining_status: str = Form(""),
    allow_network: bool = Form(False),
) -> HTMLResponse:
    try:
        rules = json.loads(rules_json)
    except json.JSONDecodeError:
        return invalid_action(request, "Steam 规则 JSON 无效。")
    if not isinstance(rules, list):
        return invalid_action(request, "Steam 规则必须是列表。")
    try:
        save_steam_rule_configuration(
            request.app.state.settings.steam_config,
            {
                "rules": rules,
                "remaining_status": remaining_status or None,
                "allow_network": allow_network,
            },
        )
    except (SettingsEditError, SteamConfigurationError) as exc:
        return invalid_action(request, str(exc))
    return _restart_redirect("steam-rules")


@router.post("/auth-check")
def auth_check(session: Session = Depends(get_write_session)) -> RedirectResponse:
    job = enqueue_job(
        session,
        kind="auth_check",
        capability="remote_read",
        config={},
    )
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)

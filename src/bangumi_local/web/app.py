from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import threading

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import sessionmaker
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from bangumi_local import __version__
from bangumi_local.config import Settings, get_settings
from bangumi_local.db.session import create_database_engine
from bangumi_local.web.routes import (
    dashboard,
    discovery,
    external,
    health,
    help as help_routes,
    jobs,
    media,
    plan_actions,
    plans,
    rating,
    settings as settings_routes,
    steam,
    sync,
    tags,
    works,
    workspace,
)
from bangumi_local.web.security import LocalSecurityMiddleware
from bangumi_local.web.presentation import plan_reason_label, status_label
from bangumi_local.services.jobs import interrupt_running_jobs, sanitize_job_payload
from bangumi_local.services.ui_jobs import build_ui_job_runner


PACKAGE_DIRECTORY = Path(__file__).resolve().parent


def create_app(
    settings: Settings | None = None,
    *,
    start_worker: bool = False,
    env_file: str | Path | None = None,
) -> FastAPI:
    """Build the local UI application without starting a server or running migrations."""

    runtime_settings = settings or get_settings(env_file or ".env")
    engine = create_database_engine(runtime_settings.database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop_event: threading.Event | None = None
        worker: threading.Thread | None = None
        if start_worker:
            with factory.begin() as session:
                interrupt_running_jobs(session)
            runner = build_ui_job_runner(runtime_settings)
            stop_event = threading.Event()
            worker = threading.Thread(
                target=runner.run_forever,
                args=(stop_event,),
                name="bld-ui-job-worker",
                daemon=True,
            )
            worker.start()
        try:
            yield
        finally:
            if stop_event is not None:
                stop_event.set()
            if worker is not None:
                worker.join(timeout=5)
            engine.dispose()

    application = FastAPI(
        title="Bangumi Local Database",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.settings = runtime_settings
    application.state.settings_env_file = (
        Path(env_file) if env_file is not None else (Path(".env") if settings is None else None)
    )
    application.state.session_factory = factory
    templates = Jinja2Templates(directory=PACKAGE_DIRECTORY / "templates")
    templates.env.autoescape = True
    templates.env.filters["status_label"] = status_label
    templates.env.filters["plan_reason_label"] = plan_reason_label
    templates.env.globals["app_version"] = __version__
    application.state.templates = templates

    def _html_request(request: Request) -> bool:
        return "text/html" in request.headers.get("accept", "").casefold()

    def _error_suggestion(status_code: int) -> str:
        if status_code == 405:
            return (
                "请求方法与页面操作不匹配。请重启本地 UI 以确保 Python 路由与页面资源版本一致；"
                "若重启后仍出现，请从工作台重新打开该任务或计划。"
            )
        if status_code == 409:
            return "当前本地状态与操作前提不一致。请查看任务或计划状态，并在需要时生成 fresh 计划。"
        if status_code == 422:
            return "提交内容未通过验证。请返回上一页核对必填项、选择器和完整确认 ID。"
        if status_code == 404:
            return "目标不存在或已被 successor、归档或删除。请从工作台重新定位当前记录。"
        return "请返回上一页重试；若问题持续存在，请在工作台查看相关任务事件。"

    @application.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = (
            str(sanitize_job_payload(exc.detail))
            if isinstance(exc.detail, str)
            else "请求未能完成。"
        )
        if not _html_request(request):
            return JSONResponse(
                {"detail": detail}, status_code=exc.status_code, headers=exc.headers
            )
        return templates.TemplateResponse(
            request=request,
            name="actions/error.html",
            context={
                "page_title": "操作未完成",
                "detail": detail,
                "status_code": exc.status_code,
                "request_method": request.method,
                "request_path": request.url.path,
                "suggestion": _error_suggestion(exc.status_code),
            },
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        safe_errors = [
            {"location": ".".join(str(item) for item in error.get("loc", ())),
             "message": str(error.get("msg", "invalid value")),
             "type": str(error.get("type", "validation_error"))}
            for error in exc.errors()
        ]
        if not _html_request(request):
            return JSONResponse({"detail": safe_errors}, status_code=422)
        detail = "；".join(
            f"{error['location'] or '提交内容'}：{error['message']}" for error in safe_errors
        ) or "提交内容未通过验证。"
        return templates.TemplateResponse(
            request=request,
            name="actions/error.html",
            context={
                "page_title": "输入验证失败",
                "detail": detail,
                "status_code": 422,
                "request_method": request.method,
                "request_path": request.url.path,
                "suggestion": _error_suggestion(422),
            },
            status_code=422,
        )

    application.add_middleware(LocalSecurityMiddleware)
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"],
        www_redirect=False,
    )
    application.mount(
        "/static",
        StaticFiles(directory=PACKAGE_DIRECTORY / "static"),
        name="static",
    )
    application.include_router(dashboard.router)
    application.include_router(works.router)
    application.include_router(plans.router)
    application.include_router(health.router)
    application.include_router(help_routes.router)
    application.include_router(external.router)
    application.include_router(media.router)
    application.include_router(sync.router)
    application.include_router(tags.router)
    application.include_router(plan_actions.router)
    application.include_router(steam.router)
    application.include_router(rating.router)
    application.include_router(discovery.router)
    application.include_router(workspace.router)
    application.include_router(jobs.router)
    application.include_router(settings_routes.router)
    return application

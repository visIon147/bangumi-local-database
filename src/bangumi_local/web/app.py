from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import threading

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import sessionmaker
from starlette.middleware.trustedhost import TrustedHostMiddleware

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
)
from bangumi_local.web.security import LocalSecurityMiddleware
from bangumi_local.services.jobs import interrupt_running_jobs
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
    application.state.templates = templates

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
    application.include_router(jobs.router)
    application.include_router(settings_routes.router)
    return application

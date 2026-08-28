from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse


def job_required(
    request: Request,
    *,
    action: str,
    detail: str,
) -> HTMLResponse:
    """Fail closed until the persistent background-job boundary is available."""

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="actions/job_required.html",
        context={"page_title": "需要后台任务", "action": action, "detail": detail},
        status_code=503,
        headers={"X-BLD-Action": "job-required"},
    )


def invalid_action(request: Request, detail: str, *, status_code: int = 422) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="actions/error.html",
        context={"page_title": "操作被拒绝", "detail": detail},
        status_code=status_code,
    )

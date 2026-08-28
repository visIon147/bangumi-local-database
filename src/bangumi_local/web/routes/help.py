from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup

from bangumi_local.web.documents import (
    PublicDocumentError,
    load_public_document,
    public_document_catalog,
)


router = APIRouter(prefix="/help")


@router.get("", response_class=HTMLResponse)
def help_index(request: Request) -> HTMLResponse:
    try:
        documents = public_document_catalog()
    except PublicDocumentError as exc:
        raise HTTPException(503, str(exc)) from None
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="help/index.html",
        context={"documents": documents, "page_title": "帮助文档"},
    )


@router.get("/{slug}", response_class=HTMLResponse)
def help_document(slug: str, request: Request) -> HTMLResponse:
    try:
        document = load_public_document(slug)
    except PublicDocumentError as exc:
        raise HTTPException(404, str(exc)) from None
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="help/document.html",
        context={
            "document": document,
            "document_html": Markup(document.html),
            "page_title": document.title,
        },
    )

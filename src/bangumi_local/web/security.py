from __future__ import annotations

import hmac
import secrets
from ipaddress import ip_address
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response


CSRF_COOKIE = "bld_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
TEST_CLIENT_HOSTS = {"testclient"}


def _is_loopback(value: str) -> bool:
    if value in {"localhost", *TEST_CLIENT_HOSTS}:
        return True
    try:
        return ip_address(value.strip("[]")).is_loopback
    except ValueError:
        return False


def _same_origin(request: Request, raw_origin: str) -> bool:
    try:
        origin = urlsplit(raw_origin)
    except ValueError:
        return False
    if origin.scheme not in {"http", "https"} or origin.username or origin.password:
        return False
    request_host = request.headers.get("host", "").casefold()
    return origin.scheme == request.url.scheme and origin.netloc.casefold() == request_host


class LocalSecurityMiddleware(BaseHTTPMiddleware):
    """Enforce the local-only threat boundary and browser request protections."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        peer = request.client.host if request.client is not None else ""
        if not _is_loopback(peer):
            return PlainTextResponse("Loopback access only.", status_code=403)

        token = request.cookies.get(CSRF_COOKIE)
        if token is None or len(token) < 32:
            token = secrets.token_urlsafe(32)
        request.state.csrf_token = token

        if request.method.upper() not in SAFE_METHODS:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            source = origin or referer
            supplied = request.headers.get(CSRF_HEADER, "")
            if (
                source is None
                or not _same_origin(request, source)
                or not hmac.compare_digest(token, supplied)
            ):
                return PlainTextResponse("Unsafe browser request rejected.", status_code=403)

        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.cookies.get(CSRF_COOKIE) != token:
            response.set_cookie(
                CSRF_COOKIE,
                token,
                httponly=True,
                secure=request.url.scheme == "https",
                samesite="strict",
                path="/",
            )
        return response

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from bangumi_local.config import Settings


def get_settings(request: Request) -> Settings:
    """Return server-side settings without exposing them to templates."""

    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    """Open a read-only request-scoped ORM session.

    Read routes never commit. Mutation routes use application services with their
    own short transactions instead of reusing this dependency.
    """

    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def get_write_session(request: Request) -> Iterator[Session]:
    """Open a short local-mutation transaction; callers must not perform network I/O."""

    session: Session = request.app.state.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

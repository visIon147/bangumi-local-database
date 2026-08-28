from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    ChangePlan,
    PlanConfirmationNonce,
    UiJob,
    UiJobEvent,
)
from bangumi_local.db.repositories import utc_now_iso
from bangumi_local.db.session import session_scope


class JobError(ValueError):
    pass


class JobCancelled(RuntimeError):
    pass


_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "bangumi_access_token",
    "password",
    "path",
    "private_reason",
    "reason_private",
    "secret",
    "steam_root",
    "steam_web_api_key",
    "token",
}
_WINDOWS_PATH = re.compile(r"(?i)\b[a-z]:\\[^\r\n]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+\-/]+=*")


def _safe_text(value: object, *, limit: int = 500) -> str:
    text = str(value)
    text = _WINDOWS_PATH.sub("[local-path]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    return text[:limit]


def sanitize_job_payload(value: object) -> object:
    """Return a JSON-safe payload with secrets, private reasons and paths removed."""

    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or any(
                marker in normalized for marker in ("token", "secret", "password")
            ):
                continue
            result[key] = sanitize_job_payload(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [sanitize_job_payload(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(value, limit=2_000)


def enqueue_job(
    session: Session,
    *,
    kind: str,
    capability: str,
    config: Mapping[str, object],
    idempotency_key: str | None = None,
) -> UiJob:
    if capability not in {"local_read", "local_write", "remote_read", "remote_write"}:
        raise JobError("Unsupported job capability.")
    if idempotency_key:
        existing = session.scalar(
            select(UiJob).where(UiJob.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing
    job = UiJob(
        id=str(uuid4()),
        kind=kind.strip(),
        capability=capability,
        status="queued",
        config_json=json.dumps(
            sanitize_job_payload(config), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        result_json=None,
        progress_current=0,
        progress_total=None,
        phase="queued",
        idempotency_key=idempotency_key,
        error_code=None,
        error_message=None,
        created_at=utc_now_iso(),
        started_at=None,
        heartbeat_at=None,
        finished_at=None,
    )
    session.add(job)
    session.flush()
    _event(session, job, "info", "Job queued.")
    return job


def _event(session: Session, job: UiJob, level: str, message: str) -> None:
    sequence = int(
        session.scalar(
            select(func.coalesce(func.max(UiJobEvent.sequence), 0)).where(
                UiJobEvent.job_id == job.id
            )
        )
        or 0
    ) + 1
    session.add(
        UiJobEvent(
            job_id=job.id,
            sequence=sequence,
            level=level,
            phase=job.phase,
            progress_current=job.progress_current,
            progress_total=job.progress_total,
            message=_safe_text(message),
            created_at=utc_now_iso(),
        )
    )


def interrupt_running_jobs(session: Session) -> int:
    rows = session.scalars(select(UiJob).where(UiJob.status == "running")).all()
    now = utc_now_iso()
    for job in rows:
        job.status = "interrupted"
        job.finished_at = now
        job.error_code = "server_restart"
        job.error_message = "Interrupted by local UI restart; review before retrying."
        _event(session, job, "warning", job.error_message)
    return len(rows)


def request_job_cancel(session: Session, job_id: str) -> UiJob:
    job = session.get(UiJob, job_id)
    if job is None:
        raise JobError(f"Job does not exist: {job_id}")
    if job.status == "queued":
        job.status = "cancelled"
        job.finished_at = utc_now_iso()
        _event(session, job, "info", "Queued job cancelled.")
    elif job.status == "running":
        job.status = "cancel_requested"
        _event(session, job, "warning", "Cancellation requested.")
    return job


@dataclass(slots=True)
class JobContext:
    database_url: str
    job_id: str

    def update(
        self,
        *,
        phase: str,
        current: int,
        total: int | None,
        message: str,
    ) -> None:
        with session_scope(self.database_url) as session:
            job = session.get(UiJob, self.job_id)
            if job is None:
                raise JobError("Job disappeared while running.")
            if job.status == "cancel_requested":
                job.status = "cancelled"
                job.finished_at = utc_now_iso()
                _event(session, job, "info", "Job cancelled at a safe checkpoint.")
                raise JobCancelled()
            job.phase = phase[:64]
            job.progress_current = max(0, current)
            job.progress_total = max(0, total) if total is not None else None
            job.heartbeat_at = utc_now_iso()
            _event(session, job, "info", message)


JobHandler = Callable[[JobContext, Mapping[str, object]], Mapping[str, object] | None]


class JobRunner:
    """One local worker; remote-write handlers additionally share one global lock."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._handlers: dict[str, JobHandler] = {}
        self._mutation_lock = threading.Lock()

    def register(self, kind: str, handler: JobHandler) -> None:
        if kind in self._handlers:
            raise JobError(f"Job handler already registered: {kind}")
        self._handlers[kind] = handler

    def run_next(self) -> str | None:
        with session_scope(self.database_url) as session:
            job = session.scalar(
                select(UiJob)
                .where(UiJob.status == "queued")
                .order_by(UiJob.created_at, UiJob.id)
                .limit(1)
            )
            if job is None:
                return None
            handler = self._handlers.get(job.kind)
            if handler is None:
                job.status = "failed"
                job.error_code = "handler_missing"
                job.error_message = "No local handler is registered for this job type."
                job.finished_at = utc_now_iso()
                _event(session, job, "error", job.error_message)
                return job.id
            job.status = "running"
            job.started_at = utc_now_iso()
            job.heartbeat_at = job.started_at
            job.phase = "starting"
            job_id = job.id
            capability = job.capability
            config = json.loads(job.config_json)
            _event(session, job, "info", "Job started.")

        context = JobContext(self.database_url, job_id)
        lock = self._mutation_lock if capability == "remote_write" else _NullLock()
        try:
            with lock:
                result = handler(context, config) or {}
        except JobCancelled:
            return job_id
        except Exception as exc:
            with session_scope(self.database_url) as session:
                job = session.get(UiJob, job_id)
                assert job is not None
                job.status = "failed"
                job.error_code = type(exc).__name__[:64]
                job.error_message = _safe_text(exc)
                job.finished_at = utc_now_iso()
                _event(session, job, "error", job.error_message)
            return job_id

        with session_scope(self.database_url) as session:
            job = session.get(UiJob, job_id)
            assert job is not None
            job.status = "succeeded"
            job.result_json = json.dumps(
                sanitize_job_payload(result), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            job.phase = "completed"
            job.finished_at = utc_now_iso()
            _event(session, job, "info", "Job completed.")
        return job_id

    def run_forever(
        self, stop_event: threading.Event, *, idle_seconds: float = 0.25
    ) -> None:
        while not stop_event.is_set():
            if self.run_next() is None:
                stop_event.wait(idle_seconds)


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


def issue_plan_confirmation(
    session: Session,
    *,
    plan_id: str,
    browser_session: str,
    ttl_seconds: int = 300,
) -> str:
    plan = session.get(ChangePlan, plan_id)
    if plan is None:
        raise JobError(f"Plan does not exist: {plan_id}")
    if plan.status not in {"reviewed", "partial"}:
        raise JobError("Plan must be reviewed before an apply confirmation can be issued.")
    raw = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    session.add(
        PlanConfirmationNonce(
            id=str(uuid4()),
            nonce_hash=hashlib.sha256(raw.encode()).hexdigest(),
            browser_session_hash=hashlib.sha256(browser_session.encode()).hexdigest(),
            plan_id=plan.id,
            plan_content_hash=plan.content_hash,
            expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z"),
            used_at=None,
            created_at=now.isoformat().replace("+00:00", "Z"),
        )
    )
    return raw


def consume_plan_confirmation(
    session: Session,
    *,
    plan_id: str,
    full_plan_id: str,
    browser_session: str,
    nonce: str,
) -> None:
    if full_plan_id != plan_id:
        raise JobError("Full plan ID confirmation does not match.")
    nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
    row = session.scalar(
        select(PlanConfirmationNonce).where(
            PlanConfirmationNonce.nonce_hash == nonce_hash,
            PlanConfirmationNonce.plan_id == plan_id,
        )
    )
    if row is None or row.used_at is not None:
        raise JobError("Apply confirmation is invalid or already used.")
    if row.browser_session_hash != hashlib.sha256(browser_session.encode()).hexdigest():
        raise JobError("Apply confirmation belongs to another browser session.")
    expires = datetime.fromisoformat(row.expires_at.replace("Z", "+00:00"))
    if expires <= datetime.now(timezone.utc):
        raise JobError("Apply confirmation has expired.")
    plan = session.get(ChangePlan, plan_id)
    if plan is None or plan.content_hash != row.plan_content_hash:
        raise JobError("Plan changed after preflight confirmation.")
    row.used_at = utc_now_iso()

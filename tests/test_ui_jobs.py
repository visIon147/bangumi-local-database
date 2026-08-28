from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from bangumi_local.db.models import Base, ChangePlan, UiJob, UiJobEvent
from bangumi_local.db.session import create_database_engine, session_scope
from bangumi_local.services.jobs import (
    JobError,
    JobRunner,
    consume_plan_confirmation,
    enqueue_job,
    interrupt_running_jobs,
    issue_plan_confirmation,
    request_job_cancel,
)


def _database(tmp_path: Path) -> str:
    url = f"sqlite:///{(tmp_path / 'jobs.sqlite3').as_posix()}"
    engine = create_database_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def test_jobs_sanitize_and_execute_outside_creation_transaction(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        job = enqueue_job(
            session,
            kind="example",
            capability="remote_read",
            config={
                "subject_id": 1,
                "token": "must-not-persist",
                "steam_root": r"D:\\Secret\\Steam",
            },
        )
        job_id = job.id

    runner = JobRunner(database_url)

    def handler(context, config):
        assert config == {"subject_id": 1}
        context.update(phase="fetch", current=1, total=1, message="Fetched item")
        return {"ok": True, "access_token": "hidden"}

    runner.register("example", handler)
    assert runner.run_next() == job_id
    with session_scope(database_url) as session:
        stored = session.get(UiJob, job_id)
        assert stored is not None and stored.status == "succeeded"
        assert json.loads(stored.result_json or "{}") == {"ok": True}
        assert "must-not-persist" not in stored.config_json
        assert len(session.scalars(select(UiJobEvent)).all()) >= 3


def test_cancel_and_restart_interruption_are_durable(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        queued = enqueue_job(
            session, kind="queued", capability="local_write", config={}
        )
        running = enqueue_job(
            session, kind="running", capability="remote_write", config={}
        )
        running.status = "running"
        queued_id, running_id = queued.id, running.id
    with session_scope(database_url) as session:
        assert request_job_cancel(session, queued_id).status == "cancelled"
        assert interrupt_running_jobs(session) == 1
    with session_scope(database_url) as session:
        assert session.get(UiJob, running_id).status == "interrupted"  # type: ignore[union-attr]


def test_plan_confirmation_is_full_id_bound_single_use(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        plan = ChangePlan(
            id="12345678-1234-1234-1234-123456789012",
            format_version=2,
            kind="sync",
            operation="patch",
            tag=None,
            old_tag=None,
            new_tag=None,
            selector_json="{}",
            summary_json="{}",
            content_hash="a" * 64,
            status="reviewed",
            created_by="manual",
            reverse_of_plan_id=None,
            created_at="2026-01-01T00:00:00Z",
            reviewed_at="2026-01-01T00:00:00Z",
            applied_at=None,
        )
        session.add(plan)
        nonce = issue_plan_confirmation(
            session, plan_id=plan.id, browser_session="browser"
        )
        plan_id = plan.id
    with session_scope(database_url) as session:
        with pytest.raises(JobError, match="Full plan ID"):
            consume_plan_confirmation(
                session,
                plan_id=plan_id,
                full_plan_id="12345678",
                browser_session="browser",
                nonce=nonce,
            )
        consume_plan_confirmation(
            session,
            plan_id=plan_id,
            full_plan_id=plan_id,
            browser_session="browser",
            nonce=nonce,
        )
    with session_scope(database_url) as session:
        with pytest.raises(JobError, match="already used"):
            consume_plan_confirmation(
                session,
                plan_id=plan_id,
                full_plan_id=plan_id,
                browser_session="browser",
                nonce=nonce,
            )

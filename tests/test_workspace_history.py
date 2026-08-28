from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from fastapi.testclient import TestClient

from bangumi_local.config import Settings
from bangumi_local.db.models import Base, ChangePlan, UiJob, UiJobPlanLink
from bangumi_local.db.session import session_scope
from bangumi_local.services.workspace_history import (
    WorkspaceHistoryError,
    archive_jobs,
    archive_plans,
    preview_job_purge,
    preview_plan_purge,
    purge_jobs,
    purge_plans,
)
from bangumi_local.web.app import create_app


NOW = "2026-08-28T00:00:00Z"


def _database(tmp_path: Path) -> str:
    url = f"sqlite:///{(tmp_path / 'workspace.sqlite3').as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def _job(identifier: str, status: str = "succeeded") -> UiJob:
    return UiJob(
        id=identifier, kind="steam_match_plan", capability="remote_read", status=status,
        config_json="{}", result_json=None, progress_current=1, progress_total=1,
        phase="completed", idempotency_key=None, error_code=None, error_message=None,
        created_at=NOW, started_at=NOW, heartbeat_at=NOW, finished_at=NOW,
    )


def _plan(identifier: str, status: str = "draft") -> ChangePlan:
    return ChangePlan(
        id=identifier, format_version=4, kind="steam_match", operation="match",
        tag=None, old_tag=None, new_tag=None, selector_json="{}", summary_json="{}",
        content_hash="0" * 64, status=status, created_by="manual",
        reverse_of_plan_id=None, created_at=NOW, reviewed_at=None, applied_at=None,
    )


def test_archive_restore_and_guarded_purge_are_atomic(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        session.add_all((_job("job-free"), _job("job-live", "running")))
    with session_scope(database_url) as session:
        with pytest.raises(WorkspaceHistoryError, match="terminal"):
            archive_jobs(session, ("job-free", "job-live"), archived=True)
    with session_scope(database_url) as session:
        assert session.get(UiJob, "job-free").archived_at is None  # type: ignore[union-attr]
        archive_jobs(session, ("job-free",), archived=True)
        assert preview_job_purge(session, ("job-free",)).purgeable == ("job-free",)
        assert purge_jobs(session, ("job-free",)) == 1
    with session_scope(database_url) as session:
        assert session.get(UiJob, "job-free") is None


def test_plan_and_linked_job_audit_references_block_purge(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        session.add_all((_job("job-linked"), _plan("plan-linked"), _plan("plan-free")))
        session.flush()
        session.add(
            UiJobPlanLink(
                job_id="job-linked", plan_id="plan-linked", relation="created", created_at=NOW
            )
        )
        archive_jobs(session, ("job-linked",), archived=True)
        archive_plans(session, ("plan-linked", "plan-free"), archived=True)
    with session_scope(database_url) as session:
        assert preview_job_purge(session, ("job-linked",)).blocked[0][1] == "仍有关联计划"
        assert preview_plan_purge(session, ("plan-linked",)).blocked[0][1] == "仍有关联任务"
        assert purge_plans(session, ("plan-free",)) == 1
        assert session.scalar(select(ChangePlan.id).where(ChangePlan.id == "plan-free")) is None


def test_workspace_page_status_and_archive_round_trip(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        session.add_all((_job("job-web"), _plan("plan-web")))
        session.flush()
        session.add(
            UiJobPlanLink(
                job_id="job-web", plan_id="plan-web", relation="created", created_at=NOW
            )
        )
    app = create_app(
        Settings(
            _env_file=None,
            database_url=database_url,
            backup_directory=tmp_path / "backups",
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        page = client.get("/workspace")
        status = client.get("/jobs/job-web/status")
        csrf = client.cookies.get("bld_csrf")
        archived = client.post(
            "/workspace/jobs/archive",
            data={"ids": "job-web"},
            headers={"origin": "http://localhost", "x-csrf-token": str(csrf)},
            follow_redirects=False,
        )
    assert page.status_code == 200 and "/plans/plan-web" in page.text
    assert status.json()["links"][0]["plan_id"] == "plan-web"
    assert archived.status_code == 303
    with session_scope(database_url) as session:
        assert session.get(UiJob, "job-web").archived_at is not None  # type: ignore[union-attr]


def test_workspace_purge_creates_backup_before_delete(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        session.add(_job("job-purge"))
        archive_jobs(session, ("job-purge",), archived=True)
    backup_directory = tmp_path / "backups"
    app = create_app(
        Settings(
            _env_file=None,
            database_url=database_url,
            backup_directory=backup_directory,
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        client.get("/workspace")
        csrf = str(client.cookies.get("bld_csrf"))
        response = client.post(
            "/workspace/jobs/purge",
            data={"ids": "job-purge", "confirmation": "DELETE 1"},
            headers={"origin": "http://localhost", "x-csrf-token": csrf},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert tuple(backup_directory.glob("*.sqlite3"))
    with session_scope(database_url) as session:
        assert session.get(UiJob, "job-purge") is None

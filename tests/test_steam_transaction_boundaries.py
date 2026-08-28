from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path

from sqlalchemy import select

from bangumi_local.config import Settings
from bangumi_local.db.models import (
    Base,
    ChangePlan,
    LibraryEntry,
    LibraryMatchCandidate,
    SourceAccount,
    UiJob,
)
from bangumi_local.db.session import create_database_engine, session_scope
from bangumi_local.domain.models import SubjectSearchCandidate, SubjectType
from bangumi_local.services.jobs import enqueue_job
from bangumi_local.services.ui_jobs import build_ui_job_runner


def _database(tmp_path: Path) -> str:
    database_url = f"sqlite:///{(tmp_path / 'boundary.sqlite3').as_posix()}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return database_url


def _seed_entries(database_url: str, app_ids: tuple[str, ...]) -> None:
    with session_scope(database_url) as session:
        account = SourceAccount(
            source="steam",
            external_account_id="123",
            account_name=None,
            config_json="{}",
            first_seen_at="2026-01-01T00:00:00Z",
            last_seen_at="2026-01-01T00:00:00Z",
        )
        session.add(account)
        session.flush()
        for app_id in app_ids:
            session.add(
                LibraryEntry(
                    source_account_id=account.id,
                    external_id=app_id,
                    work_id=None,
                    title_observed=f"Game {app_id}",
                    localized_titles_json="{}",
                    ownership_scope="owned",
                    installed=False,
                    playtime_minutes=1,
                    last_played_at=None,
                    metadata_source="test",
                    metadata_json="{}",
                    source_hash=app_id.zfill(64),
                    match_status="unmatched",
                    match_reason=None,
                    match_updated_at=None,
                    first_seen_at="2026-01-01T00:00:00Z",
                    last_seen_at="2026-01-01T00:00:00Z",
                )
            )


def _install_boundary_spies(monkeypatch, active: list[int]) -> list[float]:
    from bangumi_local.services import steam_matching, ui_jobs

    original_session_scope = ui_jobs.session_scope

    @contextmanager
    def tracked_session_scope(database_url: str):
        with original_session_scope(database_url) as session:
            active[0] += 1
            try:
                yield session
            finally:
                active[0] -= 1

    candidate = SubjectSearchCandidate(
        subject_id=900,
        subject_type=SubjectType.GAME,
        title_original="Game 100",
        title_cn=None,
        summary=None,
        release_date=None,
        cover_url=None,
        aliases=(),
        public_tags=(),
    )

    class BoundaryClient:
        def __init__(self, _settings: Settings) -> None:
            pass

        def __enter__(self):
            assert active[0] == 0
            return self

        def __exit__(self, *_args: object) -> None:
            assert active[0] == 0

        def search_subjects(self, *_args: object, **_kwargs: object):
            assert active[0] == 0, "Bangumi HTTP ran inside a database transaction"
            return (candidate,)

    def store_titles(*_args: object, **_kwargs: object) -> dict[str, str]:
        assert active[0] == 0, "Steam HTTP ran inside a database transaction"
        return {}

    sleeps: list[float] = []

    def boundary_sleep(seconds: float) -> None:
        assert active[0] == 0, "batch pacing sleep ran inside a database transaction"
        sleeps.append(seconds)

    monkeypatch.setattr(ui_jobs, "session_scope", tracked_session_scope)
    monkeypatch.setattr(ui_jobs, "BangumiClient", BoundaryClient)
    monkeypatch.setattr(steam_matching, "fetch_store_titles", store_titles)
    monkeypatch.setattr(ui_jobs.time, "sleep", boundary_sleep)
    return sleeps


def test_ui_steam_search_has_no_http_inside_transaction(tmp_path: Path, monkeypatch) -> None:
    database_url = _database(tmp_path)
    _seed_entries(database_url, ("100",))
    active = [0]
    _install_boundary_spies(monkeypatch, active)
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        plan_directory=tmp_path / "plans",
        steam_account_id="123",
    )
    runner = build_ui_job_runner(settings)
    with session_scope(database_url) as session:
        job_id = enqueue_job(
            session,
            kind="steam_match_search",
            capability="remote_read",
            config={"app_id": "100", "limit": 10},
        ).id

    assert runner.run_next() == job_id
    assert active == [0]
    with session_scope(database_url) as session:
        job = session.get(UiJob, job_id)
        assert job is not None and job.status == "succeeded"
        assert json.loads(job.result_json or "{}")["candidate_count"] == 1
        assert len(session.scalars(select(LibraryMatchCandidate)).all()) == 1


def test_ui_steam_batch_has_no_http_or_sleep_inside_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    database_url = _database(tmp_path)
    _seed_entries(database_url, ("100", "200"))
    active = [0]
    sleeps = _install_boundary_spies(monkeypatch, active)
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        plan_directory=tmp_path / "plans",
        steam_account_id="123",
    )
    runner = build_ui_job_runner(settings)
    with session_scope(database_url) as session:
        job_id = enqueue_job(
            session,
            kind="steam_match_plan",
            capability="remote_read",
            config={
                "appids": "100,200",
                "limit": 10,
                "request_delay_ms": 25,
                "auto_threshold": 95,
                "min_margin": 20,
            },
        ).id

    assert runner.run_next() == job_id
    assert active == [0]
    assert sleeps == [0.025]
    with session_scope(database_url) as session:
        job = session.get(UiJob, job_id)
        assert job is not None and job.status == "succeeded"
        result = json.loads(job.result_json or "{}")
        plan = session.get(ChangePlan, result["plan_id"])
        assert plan is not None and plan.kind == "steam_match"
        assert len(session.scalars(select(LibraryMatchCandidate)).all()) == 2


def test_ui_steam_title_completion_has_no_http_or_sleep_inside_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    from bangumi_local.services import steam_titles, ui_jobs

    database_url = _database(tmp_path)
    _seed_entries(database_url, ("100", "200"))
    with session_scope(database_url) as session:
        for entry in session.scalars(select(LibraryEntry)).all():
            entry.title_observed = None
    active = [0]
    original_session_scope = ui_jobs.session_scope

    @contextmanager
    def tracked_session_scope(database_url: str):
        with original_session_scope(database_url) as session:
            active[0] += 1
            try:
                yield session
            finally:
                active[0] -= 1

    sleeps: list[float] = []

    def fetch_title(app_id: str, **_kwargs: object) -> dict[str, str]:
        assert active[0] == 0
        return {"english": f"Store {app_id}"}

    def sleep(seconds: float) -> None:
        assert active[0] == 0
        sleeps.append(seconds)

    monkeypatch.setattr(ui_jobs, "session_scope", tracked_session_scope)
    monkeypatch.setattr(steam_titles, "fetch_store_titles", fetch_title)
    monkeypatch.setattr(ui_jobs.time, "sleep", sleep)
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        steam_account_id="123",
    )
    runner = build_ui_job_runner(settings)
    with session_scope(database_url) as session:
        job_id = enqueue_job(
            session,
            kind="steam_titles_complete",
            capability="remote_read",
            config={"all_missing": True, "max_items": 250, "request_delay_ms": 25},
        ).id
    assert runner.run_next() == job_id
    assert active == [0] and sleeps == [0.025]
    with session_scope(database_url) as session:
        titles = [entry.title_observed for entry in session.scalars(select(LibraryEntry))]
        result = json.loads(session.get(UiJob, job_id).result_json or "{}")  # type: ignore[union-attr]
        assert titles == ["Store 100", "Store 200"]
        assert result["updated"] == 2

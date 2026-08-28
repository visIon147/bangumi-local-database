from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from bangumi_local.config import Settings
from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    Base,
    ChangePlan,
    RemoteOperation,
    UiJob,
    Work,
)
from bangumi_local.db.repositories import utc_now_iso, write_shadow
from bangumi_local.domain.models import (
    CollectionStatus,
    RemoteCollection,
    RemoteSubject,
    SubjectType,
)
from bangumi_local.services.plans import create_bulk_tag_plan
from bangumi_local.services.pull import snapshot_from_remote
from bangumi_local.web import create_app
from bangumi_local.web.routes import plan_actions, sync, tags


def _build_client(tmp_path: Path) -> tuple[TestClient, str, int, str]:
    database_path = tmp_path / "actions.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    now = utc_now_iso()
    remote = RemoteCollection(
        subject_id=101,
        subject_type=SubjectType.GAME,
        status=CollectionStatus.DONE,
        rate=8,
        comment="before",
        tags=(),
        updated_at=datetime.fromisoformat("2025-01-02T03:04:05+08:00"),
        private=False,
        subject=RemoteSubject(
            subject_id=101,
            title_original="Action test",
            title_cn=None,
            summary="summary",
            release_date="2024-01-01",
            cover_url=None,
        ),
    )
    with Session(engine) as session:
        work = Work(
            kind="game",
            title="Action test",
            title_cn=None,
            title_original="Action test",
            summary="summary",
            release_date="2024-01-01",
            cover_url=None,
            created_at=now,
            updated_at=now,
            bgm_subject_id=101,
            bgm_url="https://bgm.tv/subject/101",
        )
        session.add(work)
        session.flush()
        work_id = work.id
        session.add(
            BangumiSubject(
                subject_id=101,
                work_id=work.id,
                subject_type=4,
                url="https://bgm.tv/subject/101",
                metadata_available=True,
                last_observed_at=now,
            )
        )
        session.add(
            BangumiCollectionState(
                subject_id=101,
                bgm_collection_type=2,
                rating=8,
                comment="before",
                is_private=False,
                local_updated_at=now,
            )
        )
        session.flush()
        write_shadow(session, 101, snapshot_from_remote(remote), now)
        session.flush()
        plan = create_bulk_tag_plan(
            session,
            [remote],
            operation="add",
            selector={"mode": "ids", "ids": [101]},
            detail_loader=lambda _subject_id: (),
            tag="UI action",
        )
        plan_id = plan.plan.id
        session.commit()
    engine.dispose()

    app = create_app(Settings(_env_file=None, database_url=database_url))
    app.include_router(sync.router)
    app.include_router(tags.router)
    app.include_router(plan_actions.router)
    return TestClient(app, base_url="http://localhost"), database_url, work_id, plan_id


def _csrf(client: TestClient) -> dict[str, str]:
    response = client.get("/sync")
    assert response.status_code == 200
    token = client.cookies.get("bld_csrf")
    assert token
    return {"origin": "http://localhost", "x-csrf-token": token}


def test_mutation_configuration_gets_are_read_only(tmp_path: Path) -> None:
    client, database_url, _work_id, plan_id = _build_client(tmp_path)
    engine = create_engine(database_url)
    with Session(engine) as session:
        before = (
            session.scalar(select(func.count()).select_from(ChangePlan)),
            session.scalar(select(func.count()).select_from(RemoteOperation)),
        )
    with client:
        for path in (
            "/sync",
            "/sync/status",
            "/sync/collection/101/edit",
            "/tags",
            f"/plan-actions/{plan_id}",
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert "data-secure-post" in response.text or path == "/sync/status"
    with Session(engine) as session:
        after = (
            session.scalar(select(func.count()).select_from(ChangePlan)),
            session.scalar(select(func.count()).select_from(RemoteOperation)),
        )
    engine.dispose()
    assert after == before


def test_plan_workbench_revision_creates_successor_and_preserves_original_hash(
    tmp_path: Path,
) -> None:
    client, database_url, _work_id, plan_id = _build_client(tmp_path)
    with client:
        headers = _csrf(client)
        page = client.get(f"/plans/{plan_id}")
        assert page.status_code == 200
        assert "successor 中保留此项" in page.text
        response = client.post(
            f"/plans/{plan_id}/revise",
            data={"include_subject": "101"},
            headers=headers,
            follow_redirects=False,
        )
        assert response.status_code == 303
        successor_id = response.headers["location"].split("/plans/", 1)[1].split("?", 1)[0]
    engine = create_engine(database_url)
    with Session(engine) as session:
        original = session.get(ChangePlan, plan_id)
        successor = session.get(ChangePlan, successor_id)
        assert original is not None and original.status == "cancelled"
        assert successor is not None and successor.status == "draft"
        assert plan_id in successor.selector_json
    engine.dispose()


def test_local_collection_edit_uses_csrf_and_never_creates_remote_audit(tmp_path: Path) -> None:
    client, database_url, _work_id, _plan_id = _build_client(tmp_path)
    with client:
        headers = _csrf(client)
        rejected = client.post(
            "/sync/collection/101/edit",
            data={"rating": "9", "status": "doing"},
            headers={"origin": "http://localhost", "x-csrf-token": "wrong"},
            follow_redirects=False,
        )
        assert rejected.status_code == 403
        response = client.post(
            "/sync/collection/101/edit",
            data={
                "rating": "9",
                "status": "doing",
                "comment": "local edit",
                "privacy": "private",
            },
            headers=headers,
            follow_redirects=False,
        )
        assert response.status_code == 303
    engine = create_engine(database_url)
    with Session(engine) as session:
        state = session.get(BangumiCollectionState, 101)
        assert state is not None
        assert (state.rating, state.bgm_collection_type, state.comment, state.is_private) == (
            9,
            3,
            "local edit",
            True,
        )
        assert session.scalar(select(func.count()).select_from(RemoteOperation)) == 0
    engine.dispose()


def test_plan_review_requires_exact_id_and_apply_requires_bound_single_use_nonce(
    tmp_path: Path,
) -> None:
    client, database_url, _work_id, plan_id = _build_client(tmp_path)
    with client:
        headers = _csrf(client)
        mismatch = client.post(
            f"/plan-actions/{plan_id}/review",
            data={
                "confirmation_plan_id": plan_id[:-1]
                + ("0" if plan_id[-1] != "0" else "1")
            },
            headers=headers,
            follow_redirects=False,
        )
        assert mismatch.status_code == 422
        reviewed = client.post(
            f"/plan-actions/{plan_id}/review",
            data={"confirmation_plan_id": plan_id},
            headers=headers,
            follow_redirects=False,
        )
        assert reviewed.status_code == 303, reviewed.text
        missing_nonce = client.post(
            f"/plan-actions/{plan_id}/apply",
            data={"confirmation_plan_id": plan_id, "confirmation_nonce": ""},
            headers=headers,
        )
        assert missing_nonce.status_code == 422
        invalid_nonce = client.post(
            f"/plan-actions/{plan_id}/apply",
            data={"confirmation_plan_id": plan_id, "confirmation_nonce": "not-issued"},
            headers=headers,
        )
        assert invalid_nonce.status_code == 422
        preflight = client.post(
            f"/plan-actions/{plan_id}/preflight", headers=headers, follow_redirects=False
        )
        assert preflight.status_code == 303
        assert preflight.headers["location"].startswith("/jobs/")
        preflight_job_id = preflight.headers["location"].rsplit("/", 1)[-1]
        engine = create_engine(database_url)
        with Session(engine) as session:
            preflight_job = session.get(UiJob, preflight_job_id)
            assert preflight_job is not None
            preflight_job.status = "succeeded"
            preflight_job.result_json = (
                '{"plan_id":"' + plan_id
                + '","will_modify":[{"subject_id":101,"title":"Action test"}],'
                '"unchanged":[],"will_modify_count":1,"unchanged_count":0}'
            )
            session.commit()
        engine.dispose()
        job_page = client.get(f"/jobs/{preflight_job_id}")
        assert job_page.status_code == 200
        assert "继续安全确认" in job_page.text
        confirmation = client.post(
            f"/plan-actions/{plan_id}/confirmation",
            data={"preflight_job_id": preflight_job_id},
            headers=headers,
        )
        assert confirmation.status_code == 200
        assert "本次将修改" in confirmation.text
        nonce_match = re.search(
            r'name="confirmation_nonce" value="([^"]+)"', confirmation.text
        )
        assert nonce_match is not None
        nonce = nonce_match.group(1)
        accepted = client.post(
            f"/plan-actions/{plan_id}/apply",
            data={"confirmation_plan_id": plan_id, "confirmation_nonce": nonce},
            headers=headers,
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        assert accepted.headers["location"].startswith("/jobs/")
        reused = client.post(
            f"/plan-actions/{plan_id}/apply",
            data={"confirmation_plan_id": plan_id, "confirmation_nonce": nonce},
            headers=headers,
            follow_redirects=False,
        )
        assert reused.status_code == 422
    engine = create_engine(database_url)
    with Session(engine) as session:
        plan = session.get(ChangePlan, plan_id)
        assert plan is not None and plan.status == "reviewed"
        assert session.scalar(select(func.count()).select_from(RemoteOperation)) == 0
        job = session.scalar(select(UiJob).where(UiJob.kind == "plan_apply"))
        assert job is not None and job.kind == "plan_apply" and job.status == "queued"
    engine.dispose()


def test_network_plan_forms_validate_then_return_job_required_without_writes(
    tmp_path: Path,
) -> None:
    client, database_url, _work_id, _plan_id = _build_client(tmp_path)
    with client:
        headers = _csrf(client)
        invalid = client.post(
            "/tags/bulk",
            data={
                "operation": "rename",
                "old_tag": "same",
                "new_tag": "same",
                "selector_mode": "ids",
                "subject_ids": "101",
            },
            headers=headers,
        )
        assert invalid.status_code == 422
        tag_plan = client.post(
            "/tags/bulk",
            data={
                "operation": "add",
                "tag": "普通Game分类",
                "selector_mode": "public_tag",
                "public_tag": "Game",
            },
            headers=headers,
            follow_redirects=False,
        )
        assert tag_plan.status_code == 303
        pull = client.post(
            "/sync/pull",
            data={"subject_type": "game", "image_policy": "none"},
            headers=headers,
            follow_redirects=False,
        )
        assert pull.status_code == 303
        assert pull.headers["location"].startswith("/jobs/")
        sync_plan = client.post(
            "/sync/plan",
            data={
                "selector_mode": "ids",
                "subject_ids": "101",
                "fields": ["rate", "comment"],
            },
            headers=headers,
            follow_redirects=False,
        )
        assert sync_plan.status_code == 303
    engine = create_engine(database_url)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ChangePlan)) == 1
        assert session.scalar(select(func.count()).select_from(RemoteOperation)) == 0
        jobs = session.scalars(select(UiJob).order_by(UiJob.created_at, UiJob.id)).all()
        assert {job.kind for job in jobs} == {"bulk_tag_plan", "bangumi_pull_plan", "sync_plan"}
        assert all(job.status == "queued" for job in jobs)
    engine.dispose()

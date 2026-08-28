from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from bangumi_local.config import Settings
from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    Base,
    ChangePlan,
    Work,
)
from bangumi_local.db.repositories import utc_now_iso
from bangumi_local.domain.models import (
    CollectionStatus,
    RemoteCollection,
    RemoteSubject,
    SubjectType,
)
from bangumi_local.services.plans import create_bulk_tag_plan
from bangumi_local.web import create_app
from bangumi_local.web.documents import render_public_markdown


def _remote(subject_id: int, title: str) -> RemoteCollection:
    return RemoteCollection(
        subject_id=subject_id,
        subject_type=SubjectType.GAME,
        status=CollectionStatus.DONE,
        rate=8,
        comment="safe",
        tags=("RPG",),
        updated_at=datetime.fromisoformat("2025-01-02T03:04:05+08:00"),
        private=False,
        subject=RemoteSubject(
            subject_id=subject_id,
            title_original=title,
            title_cn=None,
            summary="summary",
            release_date="2024-01-01",
            cover_url="https://lain.bgm.tv/example.jpg",
        ),
    )


def _seed_database(path: Path) -> tuple[str, int, str]:
    database_url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    now = utc_now_iso()
    with Session(engine) as session:
        work = Work(
            kind="game",
            title="<script>alert('escaped')</script>",
            title_cn=None,
            title_original="Original",
            summary="<img src=x onerror=alert(1)>",
            release_date="2024-01-01",
            cover_url="https://lain.bgm.tv/example.jpg",
            created_at=now,
            updated_at=now,
            bgm_subject_id=101,
            bgm_url="https://bgm.tv/subject/101",
        )
        session.add(work)
        session.flush()
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
                comment="safe",
                is_private=False,
                local_updated_at=now,
            )
        )
        session.flush()
        remote = _remote(101, work.title)
        stored = create_bulk_tag_plan(
            session,
            [remote],
            operation="add",
            selector={"mode": "ids", "ids": [101]},
            detail_loader=lambda _subject_id: (),
            tag="UI test",
        )
        work_id = work.id
        plan_id = stored.plan.id
        session.commit()
    engine.dispose()
    return database_url, work_id, plan_id


def _client(tmp_path: Path) -> tuple[TestClient, str, int, str]:
    database_url, work_id, plan_id = _seed_database(tmp_path / "web.sqlite3")
    secret = "server-only-secret-value"
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        bangumi_access_token=SecretStr(secret),
        bangumi_username="tester",
        bangumi_user_agent="tester/bld-test",
    )
    return (
        TestClient(create_app(settings), base_url="http://localhost"),
        secret,
        work_id,
        plan_id,
    )


def test_read_only_pages_escape_content_and_do_not_leak_settings(tmp_path: Path) -> None:
    client, secret, work_id, plan_id = _client(tmp_path)
    with client:
        dashboard = client.get("/")
        works = client.get("/works")
        detail = client.get(f"/works/{work_id}")
        plans = client.get("/plans")
        plan = client.get(f"/plans/{plan_id}")
        health = client.get("/health")
        settings_page = client.get("/settings")
        help_index = client.get("/help")
        ui_guide = client.get("/help/ui-guide")
        readme = client.get("/help/readme")
        steam_setup = client.get("/help/steam-setup")

    ordinary_pages = (
        dashboard,
        works,
        detail,
        plans,
        plan,
        health,
        settings_page,
    )
    document_pages = (
        help_index,
        ui_guide,
        readme,
        steam_setup,
    )
    for response in ordinary_pages + document_pages:
        assert response.status_code == 200
        assert secret not in response.text
        assert (tmp_path / "web.sqlite3").as_posix() not in response.text
        assert "access-control-allow-origin" not in response.headers
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
    for response in ordinary_pages:
        assert "sqlite:///" not in response.text
    assert "<script>alert('escaped')</script>" not in works.text
    assert "&lt;script&gt;alert" in works.text
    assert "<img src=x onerror=alert(1)>" not in detail.text
    assert plan_id in plan.text and "UI test" in plan.text
    assert health.json()["database"] == "reachable"
    assert "server-only-secret-value" not in settings_page.text
    assert "设置与诊断" in settings_page.text
    assert "UI 使用指南" in help_index.text
    assert "Bangumi Local Database UI 使用指南" in ui_guide.text
    assert "本地图像数据库" in readme.text
    assert "Steam 本地库与 Web API 配置指南" in steam_setup.text


def test_help_markdown_disables_embedded_html_and_unsafe_links() -> None:
    rendered = render_public_markdown(
        '<script>alert("x")</script>\n\n[unsafe](javascript:alert(1))\n\n'
        '[external](https://example.com/)\n\n[guide](UI_GUIDE.md)\n\n'
        '[steam](STEAM_SETUP.md)\n'
    )
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert 'href="javascript:' not in rendered
    assert 'href="https://example.com/" target="_blank" rel="noopener noreferrer"' in rendered
    assert 'href="/help/ui-guide"' in rendered
    assert 'href="/help/steam-setup"' in rendered


def test_host_origin_and_csrf_are_enforced(tmp_path: Path) -> None:
    client, _secret, _work_id, _plan_id = _client(tmp_path)
    with client:
        assert client.get("/", headers={"host": "attacker.example"}).status_code == 400
        page = client.get("/")
        csrf = client.cookies.get("bld_csrf")
        assert csrf and "HttpOnly" in page.headers["set-cookie"]
        assert "SameSite=strict" in page.headers["set-cookie"]

        assert client.post("/").status_code == 403
        assert client.post(
            "/", headers={"origin": "http://attacker.example", "x-csrf-token": csrf}
        ).status_code == 403
        assert client.post(
            "/", headers={"origin": "http://localhost", "x-csrf-token": "wrong"}
        ).status_code == 403
        # Security validation succeeds, then routing rejects the unsupported method.
        assert client.post(
            "/", headers={"origin": "http://localhost", "x-csrf-token": csrf}
        ).status_code == 405


def test_all_get_routes_leave_database_counts_unchanged(tmp_path: Path) -> None:
    client, _secret, work_id, plan_id = _client(tmp_path)
    database_path = tmp_path / "web.sqlite3"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with Session(engine) as session:
        before = (
            session.scalar(select(func.count()).select_from(Work)),
            session.scalar(select(func.count()).select_from(ChangePlan)),
        )
    with client:
        for path in ("/", "/works", f"/works/{work_id}", "/plans", f"/plans/{plan_id}", "/health", "/settings", "/help", "/help/ui-guide", "/help/readme", "/help/steam-setup", "/media/placeholder"):
            assert client.get(path).status_code == 200
    with Session(engine) as session:
        after = (
            session.scalar(select(func.count()).select_from(Work)),
            session.scalar(select(func.count()).select_from(ChangePlan)),
        )
    engine.dispose()
    assert after == before


def test_tampered_plan_is_not_rendered(tmp_path: Path) -> None:
    client, _secret, _work_id, plan_id = _client(tmp_path)
    database_path = tmp_path / "web.sqlite3"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with Session(engine) as session:
        plan = session.get(ChangePlan, plan_id)
        assert plan is not None
        plan.selector_json = '{"mode":"all_current"}'
        session.commit()
    engine.dispose()
    with client:
        response = client.get(f"/plans/{plan_id}")
    assert response.status_code == 409
    assert "immutable content hash verification" in response.text.casefold()

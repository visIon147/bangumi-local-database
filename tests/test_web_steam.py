from __future__ import annotations

from pathlib import Path
import json

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from bangumi_local.adapters.steam import (
    SteamCollectionRecord,
    SteamDetection,
    SteamEntryRecord,
    SteamSnapshot,
)
from bangumi_local.config import Settings
from bangumi_local.db.models import (
    Base,
    ChangePlan,
    LibraryCollection,
    LibraryEntry,
    LibraryEntryCollection,
    LibraryMatchCandidate,
    LibraryMatchReview,
    SourceAccount,
)
from bangumi_local.db.repositories import utc_now_iso
from bangumi_local.domain.plans import stable_json
from bangumi_local.services.steam_match_plans import create_steam_match_plan
from bangumi_local.web import create_app
from bangumi_local.web.routes import steam


def _seed(path: Path) -> tuple[str, str]:
    database_url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    now = utc_now_iso()
    with Session(engine) as session:
        account = SourceAccount(
            source="steam",
            external_account_id="private-account-123",
            account_name="private-name",
            config_json='{"root":"D:/private/Steam"}',
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(account)
        session.flush()
        entry = LibraryEntry(
            source_account_id=account.id,
            external_id="70",
            title_observed="Half-Life <script>alert(1)</script>",
            localized_titles_json="{}",
            ownership_scope="installed",
            installed=True,
            playtime_minutes=720,
            metadata_source="steam_local_cache",
            metadata_json="{}",
            source_hash="a" * 64,
            match_status="no_subject",
            match_reason="test seed",
            first_seen_at=now,
            last_seen_at=now,
        )
        collection = LibraryCollection(
            source_account_id=account.id,
            external_id="uc-finished",
            name="完结-已购",
            kind="manual",
            active=True,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add_all((entry, collection))
        session.flush()
        session.add(
            LibraryEntryCollection(
                library_entry_id=entry.id,
                collection_id=collection.id,
                active=True,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        session.add(
            LibraryMatchCandidate(
                library_entry_id=entry.id,
                subject_id=26943,
                query="Half-Life",
                rank=1,
                score=100,
                reasons_json=stable_json(["title_similarity=1.000", "normalized_title_exact"]),
                snapshot_json=stable_json(
                    {
                        "subject_id": 26943,
                        "title": "半衰期",
                        "title_original": "Half-Life",
                        "release_date": "1998-11-19",
                        "cover_url": "https://lain.bgm.tv/cover.jpg",
                        "aliases": ["半条命"],
                        "url": "https://bgm.tv/subject/26943",
                    }
                ),
                observed_at=now,
            )
        )
        plan = create_steam_match_plan(
            session,
            None,  # no network is reached for an explicit no-subject entry
            account_id=None,
            app_ids=("70",),
            include_store_titles=False,
        )
        session.commit()
        plan_id = plan.plan.id
    engine.dispose()
    return database_url, plan_id


def _client(tmp_path: Path) -> tuple[TestClient, str, str]:
    database_url, plan_id = _seed(tmp_path / "steam-web.sqlite3")
    secret = "steam-web-server-secret"
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        bangumi_access_token=SecretStr(secret),
        bangumi_username="tester",
        bangumi_user_agent="tester/bld-ui",
        steam_account_id="private-account-123",
    )
    application = create_app(settings)
    application.include_router(steam.router)
    return TestClient(application, base_url="http://localhost"), secret, plan_id


def _csrf(client: TestClient) -> dict[str, str]:
    client.get("/steam")
    token = client.cookies.get("bld_csrf")
    assert token
    return {"origin": "http://localhost", "x-csrf-token": token}


def test_steam_get_workbench_is_read_only_escaped_and_redacted(
    tmp_path: Path, monkeypatch
) -> None:
    client, secret, _plan_id = _client(tmp_path)
    private_root = tmp_path / "private-steam-root"
    monkeypatch.setattr(
        steam,
        "detect_steam",
        lambda _settings: SteamDetection(
            root=private_root,
            account_ids=("private-account-123",),
            selected_account_id="private-account-123",
            category_source="steam_cloudstorage",
            category_file_available=True,
            legacy_file_available=True,
            local_config_available=True,
            installed_manifest_count=1,
        ),
    )
    database_path = tmp_path / "steam-web.sqlite3"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with Session(engine) as session:
        before = (
            session.scalar(select(func.count()).select_from(LibraryEntry)),
            session.scalar(select(func.count()).select_from(ChangePlan)),
            session.scalar(select(func.count()).select_from(LibraryMatchReview)),
        )
    paths = (
        "/steam", "/steam/detect", "/steam/import", "/steam/collections",
        "/steam/library", "/steam/unmatched", "/steam/match/70",
        "/steam/match/plan", "/steam/status-plan", "/steam/titles",
    )
    with client:
        responses = [client.get(path) for path in paths]
    with Session(engine) as session:
        after = (
            session.scalar(select(func.count()).select_from(LibraryEntry)),
            session.scalar(select(func.count()).select_from(ChangePlan)),
            session.scalar(select(func.count()).select_from(LibraryMatchReview)),
        )
    engine.dispose()

    assert all(response.status_code == 200 for response in responses)
    rendered = "\n".join(response.text for response in responses)
    assert secret not in rendered
    assert "private-account-123" not in rendered
    assert str(private_root) not in rendered
    assert "D:/private/Steam" not in rendered
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;alert" in rendered
    assert "100 分" in rendered and "自动候选" in rendered
    assert "normalized_title_exact" in rendered
    assert after == before


def test_import_preview_reads_snapshot_before_read_only_database_session(
    tmp_path: Path, monkeypatch
) -> None:
    client, _secret, _plan_id = _client(tmp_path)
    snapshot = SteamSnapshot(
        account_id="private-account-123",
        source_kind="test_local",
        collections=(SteamCollectionRecord("uc-finished", "完结-已购", "manual", ("70",)),),
        entries=(
            SteamEntryRecord(
                app_id="70",
                title="Half-Life",
                ownership_scope="installed",
                installed=True,
                collection_ids=("uc-finished",),
            ),
        ),
        category_snapshot_complete=True,
    )
    calls: list[bool] = []
    monkeypatch.setattr(
        steam,
        "read_steam_snapshot",
        lambda _settings, *, allow_network: calls.append(allow_network) or snapshot,
    )
    with client:
        response = client.post(
            "/steam/import/preview",
            data={"allow_network": "false"},
            headers=_csrf(client),
        )
    assert response.status_code == 200
    assert calls == [False]
    assert "LOCAL writes performed: 0" in response.text


def test_network_and_plan_operations_submit_safe_non_writing_job_requests(
    tmp_path: Path,
) -> None:
    client, _secret, _plan_id = _client(tmp_path)
    database_path = tmp_path / "steam-web.sqlite3"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with Session(engine) as session:
        before = session.scalar(select(func.count()).select_from(ChangePlan))
    with client:
        headers = _csrf(client)
        search = client.post(
            "/steam/jobs/match-search",
            data={"app_id": "70", "query": "Half-Life", "limit": "10", "allow_network": "true"},
            headers=headers,
        )
        batch = client.post(
            "/steam/jobs/match-plan",
            data={
                "all_unmatched": "true", "auto_threshold": "96", "min_margin": "21",
                "allow_nonexact_auto": "true", "limit": "12", "offset": "2",
                "max_items": "30", "request_delay_ms": "300",
                "include_no_subject": "true", "candidate_image_policy": "none",
                "allow_network": "true",
            },
            headers=headers,
        )
        titles = client.post(
            "/steam/jobs/titles-complete",
            data={
                "all_missing": "true", "max_items": "250",
                "request_delay_ms": "250", "allow_network": "true",
            },
            headers=headers,
        )
        status = client.post(
            "/steam/jobs/status-plan",
            data={"appids": "70", "remaining_status": "doing", "add_tag": "普通Game分类"},
            headers=headers,
        )
        confirm = client.post(
            "/steam/jobs/match-confirm",
            data={"app_id": "70", "subject_id": "26943", "allow_network": "true"},
            headers=headers,
        )
    with Session(engine) as session:
        after = session.scalar(select(func.count()).select_from(ChangePlan))
    engine.dispose()

    for response, kind in (
        (search, "steam_match_search"), (batch, "steam_match_plan"),
        (titles, "steam_titles_complete"), (status, "steam_status_plan"),
        (confirm, "steam_match_confirm"),
    ):
        assert response.status_code == 202
        assert kind in response.text
        assert "远端写入" in response.text and "禁止" in response.text
    assert "96" in batch.text and "21" in batch.text and "300" in batch.text
    assert "include_no_subject" in batch.text and "candidate_image_policy" in batch.text
    assert "普通Game分类" in status.text
    assert after == before


def test_manual_title_ui_is_local_and_preserved(tmp_path: Path) -> None:
    client, _secret, _plan_id = _client(tmp_path)
    database_path = tmp_path / "steam-web.sqlite3"
    with client:
        response = client.post(
            "/steam/titles/70/manual",
            data={"title": "Manual Half-Life"},
            headers=_csrf(client),
            follow_redirects=False,
        )
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with Session(engine) as session:
        entry = session.scalar(select(LibraryEntry).where(LibraryEntry.external_id == "70"))
        assert entry is not None and entry.title_observed == "Manual Half-Life"
        assert json.loads(entry.metadata_json)["manual_title"] == "Manual Half-Life"
    engine.dispose()
    assert response.status_code == 303


def test_local_dispositions_and_offline_revision_use_short_transactions(
    tmp_path: Path,
) -> None:
    client, _secret, plan_id = _client(tmp_path)
    database_path = tmp_path / "steam-web.sqlite3"
    with client:
        headers = _csrf(client)
        revised = client.post(
            "/steam/match/revise",
            data={"plan_id": plan_id, "app_id": "70", "manual_review": "true"},
            headers=headers,
            follow_redirects=False,
        )
        reopened = client.post(
            "/steam/match/70/reopen",
            data={"reason": "retry locally"},
            headers=headers,
            follow_redirects=False,
        )
        deferred = client.post(
            "/steam/match/70/defer",
            data={"reason": "later"},
            headers=headers,
            follow_redirects=False,
        )
        no_subject = client.post(
            "/steam/match/70/no-subject",
            data={"reason": "checked"},
            headers=headers,
            follow_redirects=False,
        )
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with Session(engine) as session:
        entry = session.scalar(select(LibraryEntry).where(LibraryEntry.external_id == "70"))
        plan_count = session.scalar(select(func.count()).select_from(ChangePlan))
        reviews = session.scalar(select(func.count()).select_from(LibraryMatchReview))
    engine.dispose()

    assert revised.status_code == 303 and revised.headers["location"].startswith("/plans/")
    assert reopened.status_code == deferred.status_code == no_subject.status_code == 303
    assert entry is not None and entry.match_status == "no_subject"
    assert plan_count == 2
    assert reviews == 3


def test_match_plan_workbench_uses_mutually_exclusive_decision_forms(tmp_path: Path) -> None:
    client, _secret, plan_id = _client(tmp_path)
    with client:
        page = client.get(f"/plans/{plan_id}")
        headers = _csrf(client)
        response = client.post(
            f"/steam/match/plan/{plan_id}/revise",
            data={"app_id": "70", "decision": "no_subject"},
            headers=headers,
            follow_redirects=False,
        )
    assert page.status_code == 200
    assert 'name="decision" value="no_subject"' in page.text
    assert 'name="decision" value="manual_review"' in page.text
    assert response.status_code == 303


def test_match_revision_preserves_filters_and_duplicate_submit_is_idempotent(
    tmp_path: Path,
) -> None:
    client, _secret, plan_id = _client(tmp_path)
    return_query = "q=Half&disposition=&reason=&page_size=50&page=1"
    with client:
        page = client.get(f"/plans/{plan_id}?{return_query}")
        headers = _csrf(client)
        first = client.post(
            f"/steam/match/plan/{plan_id}/revise",
            data={
                "app_id": "70",
                "decision": "no_subject",
                "return_query": return_query,
                "return_anchor": "1",
            },
            headers=headers,
            follow_redirects=False,
        )
        duplicate = client.post(
            f"/steam/match/plan/{plan_id}/revise",
            data={
                "app_id": "70",
                "decision": "no_subject",
                "return_query": return_query,
                "return_anchor": "1",
            },
            headers=headers,
            follow_redirects=False,
        )

    assert page.status_code == 200
    assert "筛选项说明" in page.text
    assert "候选需要人工核对" in page.text or "确认无条目" in page.text
    assert first.status_code == duplicate.status_code == 303
    assert first.headers["location"] == duplicate.headers["location"]
    assert "q=Half" in first.headers["location"]
    assert "page_size=50" in first.headers["location"]
    assert "notice=match-no_subject" in first.headers["location"]
    assert first.headers["location"].endswith("#plan-item-1")

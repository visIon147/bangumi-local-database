from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from bangumi_local.config import Settings
from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    Base,
    DiscoveryCandidate,
    DiscoveryReviewEvent,
    DiscoveryReviewState,
    DiscoverySession,
    LibraryEntry,
    MediaBlob,
    MediaBinding,
    MediaSource,
    RatingReviewState,
    SourceAccount,
    UiJob,
    Work,
)
from bangumi_local.db.repositories import utc_now_iso
from bangumi_local.web.routes import discovery, media, rating
from bangumi_local.web.routes.media import local_media_url
from bangumi_local.web.security import LocalSecurityMiddleware


WEB_ROOT = Path(__file__).parents[1] / "src" / "bangumi_local" / "web"


def _seed(path: Path) -> str:
    database_url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    now = utc_now_iso()
    with Session(engine) as session:
        work = Work(
            kind="game",
            title="Queue Game",
            title_cn=None,
            title_original="Queue Game",
            summary="Public summary",
            release_date="2024-01-01",
            cover_url=None,
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
                rating=None,
                comment=None,
                is_private=False,
                local_updated_at=now,
            )
        )
        account = SourceAccount(
            source="steam",
            external_account_id="local-account",
            config_json="{}",
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(account)
        session.flush()
        session.add(
            LibraryEntry(
                source_account_id=account.id,
                external_id="70",
                title_observed="Half-Life",
                localized_titles_json="{}",
                ownership_scope="owned",
                installed=True,
                playtime_minutes=60,
                metadata_json="{}",
                source_hash="a" * 64,
                match_status="unmatched",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        session.commit()
    engine.dispose()
    return database_url


def _client(tmp_path: Path) -> tuple[TestClient, Path, str]:
    database_path = tmp_path / "web-workflows.sqlite3"
    database_url = _seed(database_path)
    media_cache = tmp_path / "media-cache"
    steam_root = tmp_path / "Steam"
    (steam_root / "appcache" / "librarycache").mkdir(parents=True)
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        media_cache_directory=media_cache,
        backup_directory=tmp_path / "backups",
        steam_root=steam_root,
    )
    engine = create_engine(database_url)
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    app.state.templates = Jinja2Templates(directory=WEB_ROOT / "templates")
    app.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")
    app.add_middleware(LocalSecurityMiddleware)
    app.include_router(media.router)
    app.include_router(rating.router)
    app.include_router(discovery.router)
    return TestClient(app, base_url="http://localhost"), database_path, database_url


def _post(client: TestClient, path: str, payload: dict[str, object]):
    page = client.get("/media")
    assert page.status_code == 200
    token = client.cookies.get("bld_csrf")
    assert token
    return client.post(
        path,
        json=payload,
        headers={"origin": "http://localhost", "x-csrf-token": token},
    )


def _bind_cached_card(
    database_url: str,
    cache: Path,
    *,
    provider: str,
    external_id: str,
    work_id: int | None = None,
    library_entry_id: int | None = None,
    rating_queue_item_id: str | None = None,
    discovery_candidate_id: str | None = None,
) -> str:
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16 + external_id.encode("ascii")
    digest = hashlib.sha256(payload).hexdigest()
    target = cache / digest[:2] / f"{digest}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    engine = create_engine(database_url)
    now = utc_now_iso()
    with Session(engine) as session:
        if session.get(MediaBlob, digest) is None:
            session.add(
                MediaBlob(
                    sha256=digest,
                    storage_relpath=f"{digest[:2]}/{digest}.png",
                    mime_type="image/png",
                    byte_size=len(payload),
                    created_at=now,
                )
            )
        source = MediaSource(
            id=str(uuid4()),
            provider=provider,
            external_id=external_id,
            variant="common" if provider == "bangumi" else "library_portrait",
            locale="",
            origin="remote" if provider == "bangumi" else "steam_local",
            remote_url=None,
            logical_locator_json="{}",
            status="cached",
            current_blob_sha256=digest,
            observed_at=now,
            failure_count=0,
        )
        session.add(source)
        session.flush()
        session.add(
            MediaBinding(
                id=str(uuid4()),
                media_source_id=source.id,
                work_id=work_id,
                library_entry_id=library_entry_id,
                rating_queue_item_id=rating_queue_item_id,
                discovery_candidate_id=discovery_candidate_id,
                role="cover",
                priority=100,
                pinned_blob_sha256=digest,
                first_observed_at=now,
                last_observed_at=now,
            )
        )
        session.commit()
    engine.dispose()
    return digest


def test_card_media_resolver_uses_subject_work_and_library_bindings(tmp_path: Path) -> None:
    _client_instance, _database_path, database_url = _client(tmp_path)
    engine = create_engine(database_url)
    with Session(engine) as session:
        work_id = session.scalar(select(Work.id).where(Work.bgm_subject_id == 101))
        entry_id = session.scalar(select(LibraryEntry.id).where(LibraryEntry.external_id == "70"))
    engine.dispose()
    assert work_id is not None and entry_id is not None
    work_digest = _bind_cached_card(
        database_url,
        tmp_path / "media-cache",
        provider="bangumi",
        external_id="101",
        work_id=work_id,
    )
    library_digest = _bind_cached_card(
        database_url,
        tmp_path / "media-cache",
        provider="steam",
        external_id="70",
        library_entry_id=entry_id,
    )
    engine = create_engine(database_url)
    with Session(engine) as session:
        assert local_media_url(
            session, tmp_path / "media-cache", subject_id=101
        ) == f"/media/blob/{work_digest}"
        assert local_media_url(
            session, tmp_path / "media-cache", library_entry_id=entry_id
        ) == f"/media/blob/{library_digest}"
        assert local_media_url(
            session, tmp_path / "media-cache", subject_id=999999
        ) == "/media/placeholder"
    engine.dispose()


def test_media_get_is_local_and_blob_serving_requires_registered_safe_path(
    tmp_path: Path,
) -> None:
    client, database_path, database_url = _client(tmp_path)
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    digest = hashlib.sha256(payload).hexdigest()
    cache = tmp_path / "media-cache"
    target = cache / digest[:2] / f"{digest}.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            MediaBlob(
                sha256=digest,
                storage_relpath=f"{digest[:2]}/{digest}.png",
                mime_type="image/png",
                byte_size=len(payload),
                created_at=utc_now_iso(),
            )
        )
        session.add(
            MediaBlob(
                sha256="f" * 64,
                storage_relpath="../not-allowed.png",
                mime_type="image/png",
                byte_size=1,
                created_at=utc_now_iso(),
            )
        )
        session.commit()
    engine.dispose()

    with client:
        before = database_path.stat().st_size
        page = client.get("/media")
        status = client.get("/media/status")
        served = client.get(f"/media/blob/{digest}")
        traversal = client.get(f"/media/blob/{'f' * 64}")
        unknown = client.get(f"/media/blob/{'e' * 64}")
        after = database_path.stat().st_size
    assert page.status_code == status.status_code == served.status_code == 200
    assert status.json()["blobs"] == 2
    assert served.content == payload and served.headers["etag"] == f'"{digest}"'
    assert traversal.status_code == unknown.status_code == 404
    assert after == before


def test_media_mutations_are_explicit_persistent_jobs(tmp_path: Path) -> None:
    client, _database_path, database_url = _client(tmp_path)
    with client:
        scan = _post(client, "/media/scan", {"app_ids": ["70"], "policy": "metadata"})
        token = client.cookies.get("bld_csrf")
        assert token
        headers = {"origin": "http://localhost", "x-csrf-token": token}
        verify = client.post("/media/verify", data={}, headers=headers, follow_redirects=False)
        rejected = client.post(
            "/media/prune",
            data={"max_bytes": "0", "confirm": "wrong"},
            headers=headers,
            follow_redirects=False,
        )
        prune = client.post(
            "/media/prune",
            data={"max_bytes": "0", "confirm": "PRUNE"},
            headers=headers,
            follow_redirects=False,
        )
    assert scan.status_code == 202 and scan.json()["network_requests"] == 0
    assert verify.status_code == prune.status_code == 303
    assert rejected.status_code == 422
    engine = create_engine(database_url)
    with Session(engine) as session:
        jobs = session.scalars(select(UiJob).order_by(UiJob.created_at, UiJob.id)).all()
        assert {job.kind for job in jobs} == {
            "steam_media_scan",
            "media_verify",
            "media_prune",
        }
        assert all(job.status == "queued" for job in jobs)
    engine.dispose()


def test_rating_queue_local_actions_hide_private_reason_and_sync_is_job(
    tmp_path: Path,
) -> None:
    client, _database_path, database_url = _client(tmp_path)
    private_reason = "my private review reason"
    with client:
        created = _post(
            client,
            "/rating/queues",
            {
                "subject_types": [4],
                "collection_statuses": [2],
                "order": "title",
                "max_items": 10,
                "allow_network": False,
            },
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        next_item = client.get(f"/rating/queues/{session_id}/next")
        item_id = next_item.json()["item"]["item_id"]
        digest = _bind_cached_card(
            database_url,
            tmp_path / "media-cache",
            provider="bangumi",
            external_id="101",
            rating_queue_item_id=item_id,
        )
        page = client.get(f"/rating/queues/{session_id}")
        rated = _post(
            client,
            f"/rating/queues/{session_id}/subjects/101/rate",
            {"score": 9, "reason": private_reason, "skip_reason": False},
        )
        sync_job = _post(client, f"/rating/queues/{session_id}/sync-plan", {})
    assert page.status_code == next_item.status_code == rated.status_code == 200
    assert "Queue Game" in page.text
    assert "Bangumi 收藏：玩过（done）" in page.text
    assert f"/media/blob/{digest}" in page.text
    assert private_reason not in rated.text and private_reason not in page.text
    assert rated.json() == {"subject_id": 101, "outcome": "rated", "remote_writes": 0}
    assert sync_job.status_code == 202
    assert sync_job.json()["requires_fresh_remote"] is True
    engine = create_engine(database_url)
    with Session(engine) as session:
        state = session.get(RatingReviewState, 101)
        assert state is not None and state.reason_private == private_reason
    engine.dispose()


def test_discovery_local_decision_and_explicit_promotion_jobs(tmp_path: Path) -> None:
    client, _database_path, database_url = _client(tmp_path)
    private_reason = "local-only played evidence"
    with client:
        created = _post(
            client,
            "/discovery/sessions/steam",
            {"max_items": 10, "include_owned_unplayed": False},
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        next_item = client.get(f"/discovery/sessions/{session_id}/next").json()["candidate"]
        candidate_id = next_item["id"]
        digest = _bind_cached_card(
            database_url,
            tmp_path / "media-cache",
            provider="steam",
            external_id="70",
            discovery_candidate_id=candidate_id,
        )
        page = client.get(f"/discovery/sessions/{session_id}")
        decided = _post(
            client,
            f"/discovery/sessions/{session_id}/candidates/{candidate_id}/decide",
            {"decision": "played", "reason": private_reason},
        )
        preview = client.get(f"/discovery/candidates/{candidate_id}/promotion")
        identity = _post(
            client,
            f"/discovery/candidates/{candidate_id}/identity",
            {"subject_id": 26943, "allow_network": True},
        )
        # Simulate completion of the separate identity worker before status drafting.
        engine = create_engine(database_url)
        with Session(engine) as session:
            candidate = session.get(DiscoveryCandidate, candidate_id)
            work = session.scalar(select(Work).where(Work.bgm_subject_id == 101))
            assert candidate is not None and work is not None
            candidate.work_id = work.id
            candidate.subject_id = 101
            session.commit()
        engine.dispose()
        page_with_collection = client.get(f"/discovery/sessions/{session_id}")
        status = _post(
            client,
            f"/discovery/candidates/{candidate_id}/status-draft",
            {"status": "doing", "allow_network": True},
        )
        search = _post(
            client,
            "/discovery/sessions/search",
            {"query": "metroidvania", "max_items": 20, "allow_network": True},
        )
    assert decided.status_code == 200 and private_reason not in decided.text
    assert f"/media/blob/{digest}" in page.text
    assert "Bangumi 收藏：尚未收藏" in page.text
    assert "Bangumi 收藏：玩过（done）" in page_with_collection.text
    assert decided.json()["decision"] == "played"
    assert preview.json()["status"] == "needs_steam_match"
    assert identity.status_code == status.status_code == search.status_code == 202
    assert status.json()["explicit_collection_status"] == "doing"
    assert status.json()["inferred_from_played"] is False
    engine = create_engine(database_url)
    with Session(engine) as session:
        candidate = session.get(DiscoveryCandidate, candidate_id)
        assert candidate is not None and candidate.decision_reason == private_reason
        assert session.scalar(select(func.count()).select_from(Work)) == 1
    engine.dispose()


def test_queue_pages_use_single_card_navigation_and_enqueue_enrichment(
    tmp_path: Path,
) -> None:
    client, _database_path, database_url = _client(tmp_path)
    with client:
        rating_created = _post(
            client,
            "/rating/queues",
            {
                "subject_types": [4],
                "collection_statuses": [2],
                "order": "title",
                "max_items": 10,
                "allow_network": False,
            },
        )
        rating_id = rating_created.json()["session_id"]
        rating_page = client.get(f"/rating/queues/{rating_id}?position=99")
        rating_enrich = _post(
            client,
            f"/rating/queues/{rating_id}/enrich",
            {"scope": "current", "subject_id": 101, "cache_image": True},
        )
        discovery_created = _post(
            client,
            "/discovery/sessions/steam",
            {"max_items": 10, "include_owned_unplayed": False},
        )
        discovery_id = discovery_created.json()["session_id"]
        discovery_page = client.get(f"/discovery/sessions/{discovery_id}?position=99")
    assert rating_page.status_code == discovery_page.status_code == 200
    assert 'class="queue-card"' in rating_page.text
    assert "1 / 1" in rating_page.text
    assert "data-rating-action-panel=\"skip\"" in rating_page.text
    assert "在 Bangumi 打开" in rating_page.text
    assert 'class="queue-card"' in discovery_page.text
    assert "在 Steam 打开" in discovery_page.text
    assert rating_enrich.status_code == 202
    engine = create_engine(database_url)
    with Session(engine) as session:
        job = session.get(UiJob, rating_enrich.json()["job_id"])
        assert job is not None and job.kind == "rating_queue_enrich"
    engine.dispose()


def test_discovery_delete_requires_full_id_and_preserves_global_audit(
    tmp_path: Path,
) -> None:
    client, _database_path, database_url = _client(tmp_path)
    private_reason = "keep this global decision"
    with client:
        created = _post(
            client,
            "/discovery/sessions/steam",
            {"max_items": 10, "include_owned_unplayed": False},
        )
        session_id = created.json()["session_id"]
        candidate_id = client.get(
            f"/discovery/sessions/{session_id}/next"
        ).json()["candidate"]["id"]
        decided = _post(
            client,
            f"/discovery/sessions/{session_id}/candidates/{candidate_id}/decide",
            {"decision": "played", "reason": private_reason},
        )
        rejected = _post(
            client,
            f"/discovery/sessions/{session_id}/delete",
            {"confirm_session_id": "00000000-0000-0000-0000-000000000000"},
        )
        deleted = _post(
            client,
            f"/discovery/sessions/{session_id}/delete",
            {"confirm_session_id": session_id},
        )
    assert decided.status_code == 200
    assert rejected.status_code == 422
    assert deleted.status_code == 200
    assert deleted.json()["backup_created"] is True
    assert list((tmp_path / "backups").glob("*.sqlite3"))
    engine = create_engine(database_url)
    with Session(engine) as session:
        assert session.get(DiscoverySession, session_id) is None
        assert session.get(DiscoveryCandidate, candidate_id) is None
        review = session.scalar(select(DiscoveryReviewState))
        event = session.scalar(select(DiscoveryReviewEvent))
        assert review is not None and review.reason_private == private_reason
        assert event is not None and event.session_id is None and event.candidate_id is None
    engine.dispose()

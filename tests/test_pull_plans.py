from pathlib import Path

from sqlalchemy import create_engine

from bangumi_local.db.models import Base, BangumiCollectionState, ChangePlanItem, SyncConflict, Work
from bangumi_local.db.session import session_scope
from bangumi_local.domain.media import CachedMedia, MediaReference
from bangumi_local.services.media import register_media_references
from bangumi_local.services.plans import load_plan, review_plan
from bangumi_local.services.plan_revisions import revise_plan_selection
from bangumi_local.services.pull import pull_collections
from bangumi_local.services.pull_plans import (
    apply_pull_plan,
    create_pull_plan,
    preflight_pull_plan,
)
from bangumi_local.services.pull_media import materialize_pull_media

from conftest import make_remote_collection


def _database(tmp_path: Path) -> str:
    database_url = f"sqlite:///{(tmp_path / 'pull-plan.sqlite3').as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return database_url


def test_v5_pull_plan_imports_only_after_reviewed_apply(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    remote = make_remote_collection(subject_id=501)
    with session_scope(database_url) as session:
        stored = create_pull_plan(session, [remote], image_policy="metadata")
        assert stored.plan.format_version == 5
        assert stored.plan.kind == "pull"
        assert stored.planned[0].reason == "pull_import"
        plan_id = stored.plan.id

    with session_scope(database_url) as session:
        assert session.get(BangumiCollectionState, 501) is None
        review_plan(session, plan_id)

    fresh = preflight_pull_plan(database_url, plan_id, [remote])
    result = apply_pull_plan(
        database_url, plan_id, fresh, backup_directory=tmp_path / "backups"
    )
    assert result.status == "applied"
    assert result.applied == 1
    assert result.backup_path.is_file()
    with session_scope(database_url) as session:
        assert session.get(BangumiCollectionState, 501).rating == 8
        assert session.query(Work).count() == 1
        assert load_plan(session, plan_id).plan.status == "applied"


def test_v5_pull_plan_reports_remote_update_and_stale_local(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    original = make_remote_collection(subject_id=601, rate=6)
    changed = make_remote_collection(subject_id=601, rate=9)
    with session_scope(database_url) as session:
        pull_collections(session, [original])
    with session_scope(database_url) as session:
        stored = create_pull_plan(session, [changed])
        assert stored.planned[0].reason == "pull_remote_update"
        plan_id = stored.plan.id
        review_plan(session, plan_id)
        session.get(BangumiCollectionState, 601).comment = "local edit after plan"

    fresh = preflight_pull_plan(database_url, plan_id, [changed])
    assert not fresh.will_modify
    assert fresh.unchanged[0].reason == "stale_local"
    with session_scope(database_url) as session:
        row = session.query(ChangePlanItem).filter_by(plan_id=plan_id).one()
        assert row.item_status == "pending"


def test_pull_revision_creates_successor_without_mutating_content(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    remotes = [make_remote_collection(subject_id=701), make_remote_collection(subject_id=702)]
    with session_scope(database_url) as session:
        original = create_pull_plan(session, remotes)
        original_hash = original.plan.content_hash
        successor = revise_plan_selection(
            session, original.plan.id, included_subject_ids={701}
        )
        assert original.plan.status == "cancelled"
        assert original.plan.content_hash == original_hash
        assert len(successor.planned) == 1
        assert {item.reason for item in successor.unchanged} == {"user_excluded"}
        restored = revise_plan_selection(
            session, successor.plan.id, included_subject_ids={701, 702}
        )
        assert len(restored.planned) == 2


def test_v5_pull_conflict_records_without_overwriting_local_and_accounts_missing(
    tmp_path: Path,
) -> None:
    database_url = _database(tmp_path)
    base = make_remote_collection(subject_id=801, comment="base")
    remote = make_remote_collection(subject_id=801, comment="remote")
    with session_scope(database_url) as session:
        pull_collections(session, [base])
        session.get(BangumiCollectionState, 801).comment = "local"
    with session_scope(database_url) as session:
        stored = create_pull_plan(session, [remote])
        assert stored.planned[0].reason == "pull_conflict_record"
        plan_id = stored.plan.id
        review_plan(session, plan_id)
    fresh = preflight_pull_plan(database_url, plan_id, [remote])
    result = apply_pull_plan(
        database_url, plan_id, fresh, backup_directory=tmp_path / "backups"
    )
    assert result.applied == 1
    with session_scope(database_url) as session:
        assert session.get(BangumiCollectionState, 801).comment == "local"
        assert session.query(SyncConflict).filter_by(subject_id=801, status="open").count() == 1
        missing = create_pull_plan(session, [])
        assert missing.unchanged[0].reason == "pull_remote_missing"


def test_pull_missing_image_policy_reuses_cached_source_without_get(
    tmp_path: Path, monkeypatch,
) -> None:
    database_url = _database(tmp_path)
    remote = make_remote_collection(subject_id=901)
    reference = MediaReference(
        provider="bangumi",
        external_id="901",
        variant="preferred",
        origin="remote",
        remote_url=remote.subject.cover_url,
    )
    with session_scope(database_url) as session:
        pull_collections(session, [remote])
        register_media_references(
            session,
            (reference,),
            policy="cache",
            cached={
                reference.key: CachedMedia(
                    sha256="a" * 64,
                    storage_relpath="aa/cached.jpg",
                    mime_type="image/jpeg",
                    byte_size=10,
                    width=1,
                    height=1,
                )
            },
        )
    monkeypatch.setattr(
        "bangumi_local.services.pull_media.download_remote_media",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected GET")),
    )
    result = materialize_pull_media(
        database_url,
        [remote],
        policy="missing",
        cache_directory=tmp_path / "media",
        max_bytes=1024,
        timeout_seconds=1,
    )
    assert result.reused == 1
    assert result.downloaded == 0

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session

from conftest import make_remote_collection
from bangumi_local.db.models import Base, CollectionState, Game, GameTag, SyncConflict, SyncShadow, Tag
from bangumi_local.db.repositories import local_snapshot, shadow_snapshot
from bangumi_local.domain.merge import DiffStatus
from bangumi_local.services.pull import pull_collections
from bangumi_local.services.shadow import bootstrap_shadows
from bangumi_local.services.status import build_status_report


def _engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{(tmp_path / 'phase2.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    return engine


def test_case_a_remote_only_change_is_pulled(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session:
        pull_collections(session, [make_remote_collection(rate=8)])
        session.commit()

        before = build_status_report(session, [make_remote_collection(rate=9)])
        result = pull_collections(session, [make_remote_collection(rate=9)])
        session.commit()

        assert before.counts[DiffStatus.REMOTE_CHANGED.value] == 1
        assert result.remote_updates == 1
        assert session.scalar(select(CollectionState.rating)) == 9
        shadow = session.scalar(select(SyncShadow))
        assert shadow is not None and shadow_snapshot(shadow).rating == 9
        assert build_status_report(session, [make_remote_collection(rate=9)]).counts["clean"] == 1


def test_case_b_local_only_change_is_preserved(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session:
        pull_collections(session, [make_remote_collection(rate=8)])
        session.commit()
        state = session.scalar(select(CollectionState))
        assert state is not None
        state.rating = 9
        session.commit()

        before = build_status_report(session, [make_remote_collection(rate=8)])
        result = pull_collections(session, [make_remote_collection(rate=8)])
        session.commit()

        assert before.counts["local_changed"] == 1
        assert result.local_changes_preserved == 1
        assert state.rating == 9
        shadow = session.scalar(select(SyncShadow))
        assert shadow is not None and shadow_snapshot(shadow).rating == 8


def test_case_c_converged_change_advances_shadow(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session:
        pull_collections(session, [make_remote_collection(rate=8)])
        session.commit()
        state = session.scalar(select(CollectionState))
        assert state is not None
        state.rating = 9
        session.commit()

        before = build_status_report(session, [make_remote_collection(rate=9)])
        result = pull_collections(session, [make_remote_collection(rate=9)])
        session.commit()

        assert before.counts["converged"] == 1
        assert result.converged == 1
        shadow = session.scalar(select(SyncShadow))
        assert shadow is not None and shadow_snapshot(shadow).rating == 9
        assert build_status_report(session, [make_remote_collection(rate=9)]).counts["clean"] == 1


def test_case_d_conflict_is_persistent_and_idempotent(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session:
        pull_collections(session, [make_remote_collection(rate=8)])
        session.commit()
        state = session.scalar(select(CollectionState))
        assert state is not None
        state.rating = 9
        session.commit()

        before = build_status_report(session, [make_remote_collection(rate=7)])
        first = pull_collections(session, [make_remote_collection(rate=7)])
        session.commit()
        second = pull_collections(session, [make_remote_collection(rate=7)])
        session.commit()

        assert before.counts["conflict"] == 1
        assert first.conflicts == 1 and first.conflict_records_created == 1
        assert second.conflicts == 1 and second.conflict_records_created == 0
        assert state.rating == 9
        shadow = session.scalar(select(SyncShadow))
        assert shadow is not None and shadow_snapshot(shadow).rating == 8
        assert session.scalar(select(func.count()).select_from(SyncConflict)) == 1
        cached = build_status_report(session)
        assert cached.counts["conflict"] == 1


def test_local_only_tag_does_not_affect_snapshot_or_diff(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session:
        remote = make_remote_collection(tags=("RPG",))
        pull_collections(session, [remote])
        session.commit()
        game = session.scalar(select(Game))
        assert game is not None
        local_tag = Tag(name="pref:short-run", sync_scope="local")
        session.add(local_tag)
        session.flush()
        session.add(GameTag(work_id=game.id, tag_id=local_tag.id, origin="agent"))
        session.commit()

        assert local_snapshot(session, remote.subject_id).tags == ("RPG",)
        assert build_status_report(session, [remote]).counts["clean"] == 1


def test_shadow_bootstrap_requires_local_remote_equality(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    matching = make_remote_collection(subject_id=101, rate=8)
    mismatch = make_remote_collection(subject_id=102, rate=8)
    with Session(engine) as session:
        pull_collections(session, [matching, mismatch])
        session.commit()
        games = {
            game.bgm_subject_id: game
            for game in session.scalars(select(Game)).all()
        }
        session.delete(session.get(SyncShadow, 101))
        session.delete(session.get(SyncShadow, 102))
        states = {
            state.subject_id: state
            for state in session.scalars(select(CollectionState)).all()
        }
        states[102].rating = 9
        session.commit()

        preview = bootstrap_shadows(session, [matching, mismatch], apply=False)
        assert preview.counts["eligible"] == 1
        assert preview.counts["bootstrap_mismatch"] == 1
        assert session.scalar(select(func.count()).select_from(SyncShadow)) == 0

        applied = bootstrap_shadows(session, [matching, mismatch], apply=True)
        session.commit()
        assert applied.counts["applied"] == 1
        assert applied.counts["bootstrap_mismatch"] == 1
        assert session.scalar(select(func.count()).select_from(SyncShadow)) == 1

        repeated = bootstrap_shadows(session, [matching, mismatch], apply=True)
        assert repeated.counts["already_shadowed"] == 1
        assert repeated.counts["bootstrap_mismatch"] == 1


def test_disjoint_local_and_remote_fields_are_merged_without_overwrite(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with Session(engine) as session:
        base = make_remote_collection(rate=8, comment="base")
        pull_collections(session, [base])
        session.commit()
        state = session.scalar(select(CollectionState))
        assert state is not None
        state.rating = 9
        session.commit()

        remote = make_remote_collection(rate=8, comment="remote")
        result = pull_collections(session, [remote])
        session.commit()

        assert result.local_changes_preserved == 1
        assert result.remote_updates == 1
        assert state.rating == 9
        assert state.comment == "remote"
        shadow = session.scalar(select(SyncShadow))
        assert shadow is not None
        base_after = shadow_snapshot(shadow)
        assert base_after.rating == 8
        assert base_after.comment == "remote"

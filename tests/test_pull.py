from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from bangumi_local.db.models import Base, CollectionState, ExternalLink, Game, GameTag, SyncShadow, Tag
from bangumi_local.domain.models import RemoteGame
from bangumi_local.services.pull import pull_collections
from conftest import make_remote_collection


def test_pull_is_idempotent_with_temporary_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "mirror.sqlite3"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        first = pull_collections(session, [make_remote_collection()])
        session.commit()
        game_updated_at = session.scalar(select(Game.updated_at))
        synced_at = session.scalar(select(SyncShadow.synced_at))

        second = pull_collections(session, [make_remote_collection()])
        session.commit()

        assert first.imported == 1
        assert second.unchanged == 1
        assert session.scalar(select(func.count()).select_from(Game)) == 1
        assert session.scalar(select(func.count()).select_from(CollectionState)) == 1
        assert session.scalar(select(func.count()).select_from(Tag)) == 2
        assert session.scalar(select(func.count()).select_from(GameTag)) == 2
        assert session.scalar(select(func.count()).select_from(ExternalLink)) == 1
        assert session.scalar(select(func.count()).select_from(SyncShadow)) == 1
        assert session.scalar(select(Game.updated_at)) == game_updated_at
        assert session.scalar(select(SyncShadow.synced_at)) == synced_at


def test_pull_preserves_local_change_when_remote_matches_shadow(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'mirror.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        pull_collections(session, [make_remote_collection(rate=8)])
        session.commit()
        state = session.scalar(select(CollectionState))
        assert state is not None
        state.rating = 9
        session.commit()

        result = pull_collections(session, [make_remote_collection(rate=8)])
        session.commit()

        assert result.local_changes_preserved == 1
        assert session.scalar(select(CollectionState.rating)) == 9


def test_remote_change_updates_clean_local_and_shadow(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'mirror.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        pull_collections(session, [make_remote_collection(rate=8)])
        session.commit()
        result = pull_collections(session, [make_remote_collection(rate=9, tags=("RPG",))])
        session.commit()

        assert result.remote_updates == 1
        assert session.scalar(select(CollectionState.rating)) == 9
        assert session.scalar(select(func.count()).select_from(GameTag)) == 1


def test_missing_optional_subject_does_not_erase_existing_metadata(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'mirror.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    complete = make_remote_collection()
    without_subject = replace(
        complete,
        subject=RemoteGame(
            subject_id=complete.subject_id,
            title_original=f"Bangumi subject {complete.subject_id}",
            title_cn=None,
            summary=None,
            release_date=None,
            cover_url=None,
            metadata_available=False,
        ),
    )

    with Session(engine) as session:
        pull_collections(session, [complete])
        session.commit()
        pull_collections(session, [without_subject])
        session.commit()

        game = session.scalar(select(Game))
        assert game is not None
        assert game.title == "游戏 101"
        assert game.summary == "summary"

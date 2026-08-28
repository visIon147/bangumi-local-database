from __future__ import annotations

import json

import pytest

from bangumi_local.db.models import (
    BangumiCollectionState,
    BangumiSubject,
    Base,
    GameProfile,
    SyncConflict,
    Work,
)
from bangumi_local.db.repositories import write_shadow
from bangumi_local.db.session import create_database_engine
from bangumi_local.domain.snapshots import CollectionSnapshot
from bangumi_local.domain.models import CollectionStatus, SubjectSearchCandidate, SubjectType
from bangumi_local.services.conflicts import ConflictResolutionError, resolve_conflict
from bangumi_local.services.game_profiles import (
    GameProfileError,
    GameProfilePatch,
    edit_game_profile,
)
from bangumi_local.services.discovery import bangumi_discovery_seeds, create_discovery_session
from bangumi_local.services.discovery_promotion import (
    create_discovery_status_plan,
    promote_bangumi_identity,
)
from sqlalchemy.orm import Session


def _session() -> Session:
    engine = create_database_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _work(session: Session, *, kind: str = "game") -> Work:
    work = Work(
        kind=kind,
        title="Example",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    session.add(work)
    session.flush()
    return work


def test_keep_remote_conflict_advances_local_and_shadow() -> None:
    with _session() as session:
        work = _work(session)
        session.add(
            BangumiSubject(
                subject_id=10,
                work_id=work.id,
                subject_type=4,
                url="https://bgm.tv/subject/10",
                metadata_available=True,
                last_observed_at="2026-01-01T00:00:00Z",
            )
        )
        session.add(
            BangumiCollectionState(
                subject_id=10,
                bgm_collection_type=2,
                rating=8,
                comment=None,
                is_private=False,
                local_updated_at="2026-01-01T00:00:00Z",
            )
        )
        session.flush()
        base = CollectionSnapshot.create(
            collection_type=2, rating=6, comment=None, is_private=False, tags=()
        )
        write_shadow(session, 10, base, "2026-01-01T00:00:00Z")
        conflict = SyncConflict(
            subject_id=10,
            field="rate",
            base_json="6",
            local_json="8",
            remote_json="7",
            status="open",
            created_at="2026-01-01T00:00:00Z",
        )
        session.add(conflict)
        session.flush()

        result = resolve_conflict(session, conflict.id, strategy="keep_remote")
        assert result.local_after.rating == 7
        assert result.base_after.rating == 7
        assert conflict.status == "resolved"


def test_custom_conflict_preserves_outgoing_choice() -> None:
    with _session() as session:
        work = _work(session)
        session.add(
            BangumiSubject(
                subject_id=11,
                work_id=work.id,
                subject_type=4,
                url="https://bgm.tv/subject/11",
                metadata_available=True,
                last_observed_at="2026-01-01T00:00:00Z",
            )
        )
        session.add(
            BangumiCollectionState(
                subject_id=11,
                bgm_collection_type=2,
                rating=8,
                comment=None,
                is_private=False,
                local_updated_at="2026-01-01T00:00:00Z",
            )
        )
        session.flush()
        base = CollectionSnapshot.create(
            collection_type=2, rating=6, comment=None, is_private=False, tags=()
        )
        write_shadow(session, 11, base, "2026-01-01T00:00:00Z")
        conflict = SyncConflict(
            subject_id=11,
            field="rate",
            base_json="6",
            local_json="8",
            remote_json="7",
            status="open",
            created_at="2026-01-01T00:00:00Z",
        )
        session.add(conflict)
        session.flush()
        result = resolve_conflict(session, conflict.id, strategy="custom", custom_value=9)
        assert result.local_after.rating == 9
        assert result.base_after.rating == 7
        with pytest.raises(ConflictResolutionError):
            resolve_conflict(session, conflict.id, strategy="keep_local")


def test_game_profile_edit_is_local_and_validated() -> None:
    with _session() as session:
        work = _work(session)
        result = edit_game_profile(
            session,
            work.id,
            GameProfilePatch(
                confidence="high",
                completion="completed",
                playtime_minutes=120,
                liked_aspects=("music", "music", "story"),
                notes_private="private",
            ),
        )
        assert "confidence" in result.changed_fields
        profile = session.get(GameProfile, work.id)
        assert profile is not None
        assert json.loads(profile.liked_aspects_json or "[]") == ["music", "story"]
        assert profile.notes_private == "private"

        with pytest.raises(GameProfileError):
            edit_game_profile(session, work.id, GameProfilePatch(playtime_minutes=-1))


def test_discovery_identity_and_status_are_separate_explicit_actions() -> None:
    with _session() as session:
        subject = SubjectSearchCandidate(
            subject_id=88,
            subject_type=SubjectType.GAME,
            title_original="Discovery Game",
            title_cn=None,
            summary="summary",
            release_date="2025-01-01",
            cover_url="https://lain.bgm.tv/pic/cover/l/example.jpg",
        )
        seeds = bangumi_discovery_seeds(
            session, [subject], provider="bangumi_search", include_decided=False
        )
        view = create_discovery_session(
            session,
            provider="bangumi_search",
            filters={"query": "Discovery Game"},
            seeds=seeds,
        )
        candidate = view.candidates[0]
        promoted = promote_bangumi_identity(
            session, candidate.id, verified_subject=subject
        )
        assert promoted.created is True
        assert session.get(BangumiCollectionState, 88) is None

        stored = create_discovery_status_plan(
            session, candidate.id, status=CollectionStatus.WISH, remote=None
        )
        assert stored.plan.kind == "discovery_status"
        assert stored.planned[0].action["operation"] == "create_collection"
        assert session.get(BangumiCollectionState, 88) is None

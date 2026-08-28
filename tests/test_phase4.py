from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select

from conftest import make_remote_collection
from bangumi_local.db.models import BangumiSubject, Base, GameProfile, RemoteOperation, Work
from bangumi_local.db.repositories import local_snapshot
from bangumi_local.db.session import session_scope
from bangumi_local.domain.models import CollectionStatus, RemoteCollection, SubjectType
from bangumi_local.domain.mutations import CollectionPatch, MutationValidationError
from bangumi_local.services.apply_plan import apply_reviewed_plan, preflight_plan
from bangumi_local.services.edit_collection import edit_local_collection
from bangumi_local.services.plans import create_sync_plan, load_plan, review_plan
from bangumi_local.services.plans import create_classification_plan
from bangumi_local.domain.tags import (
    DEFAULT_GALGAME_CLASSIFICATION_TAG,
    DEFAULT_GAME_CLASSIFICATION_TAG,
)
from bangumi_local.services.pull import pull_collections


def _database(tmp_path: Path, name: str = "phase4.sqlite3") -> str:
    database_url = f"sqlite:///{(tmp_path / name).as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return database_url


def _typed_remote(subject_id: int, subject_type: SubjectType) -> RemoteCollection:
    base = make_remote_collection(subject_id=subject_id)
    return replace(
        base,
        subject_type=subject_type,
        subject=replace(base.subject, title_original=f"{subject_type.kind} {subject_id}"),
    )


class _GenericClient:
    def __init__(self, remote: RemoteCollection) -> None:
        self.remote = remote
        self.payloads: list[dict[str, object]] = []

    def get_collection(self, subject_id: int) -> RemoteCollection:
        assert subject_id == self.remote.subject_id
        return self.remote

    def patch_collection(self, subject_id: int, patch: CollectionPatch) -> None:
        assert subject_id == self.remote.subject_id
        self.payloads.append(patch.as_api_payload())
        values = patch.values
        self.remote = replace(
            self.remote,
            status=CollectionStatus(int(values.get("type", self.remote.status))),
            rate=int(values.get("rate", self.remote.rate)),
            comment=str(values["comment"]) if "comment" in values else self.remote.comment,
            private=bool(values.get("private", self.remote.private)),
            tags=tuple(values.get("tags", self.remote.tags)),  # type: ignore[arg-type]
        )


def test_mixed_subject_types_pull_idempotently_and_game_profile_is_scoped(
    tmp_path: Path,
) -> None:
    database_url = _database(tmp_path)
    remotes = [
        _typed_remote(100 + int(subject_type), subject_type)
        for subject_type in SubjectType
    ]
    with session_scope(database_url) as session:
        first = pull_collections(session, remotes)
    with session_scope(database_url) as session:
        second = pull_collections(session, remotes)
        kinds = set(session.scalars(select(Work.kind)).all())
        assert session.scalar(select(func.count()).select_from(BangumiSubject)) == 5
        assert session.scalar(select(func.count()).select_from(GameProfile)) == 1

    assert first.imported == 5
    assert first.by_subject_type == {1: 1, 2: 1, 3: 1, 4: 1, 6: 1}
    assert second.unchanged == 5
    assert kinds == {"book", "anime", "music", "game", "real"}


def test_steam_only_game_work_needs_no_bangumi_rows(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        session.add(
            Work(
                kind="game",
                title="Steam-only",
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )
        )
    with session_scope(database_url) as session:
        work = session.scalar(select(Work))
        assert work is not None
        assert work.bangumi_subject is None
        assert session.scalar(select(func.count()).select_from(BangumiSubject)) == 0


def test_filtered_pull_computes_remote_missing_only_inside_filter_scope(
    tmp_path: Path,
) -> None:
    database_url = _database(tmp_path)
    game = _typed_remote(104, SubjectType.GAME)
    anime = _typed_remote(102, SubjectType.ANIME)
    with session_scope(database_url) as session:
        pull_collections(session, [game, anime])
    with session_scope(database_url) as session:
        filtered = pull_collections(
            session, [game], scope_subject_type=SubjectType.GAME
        )
        unscoped = pull_collections(session, [game])
    assert filtered.missing_remote == 0
    assert unscoped.missing_remote == 1


def test_game_classification_excludes_non_game_works(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    game = _typed_remote(104, SubjectType.GAME)
    anime = _typed_remote(102, SubjectType.ANIME)
    with session_scope(database_url) as session:
        pull_collections(session, [game, anime])
        stored = create_classification_plan(
            session,
            [game, anime],
            public_tag="Galgame",
            galgame_tag=DEFAULT_GALGAME_CLASSIFICATION_TAG,
            game_tag=DEFAULT_GAME_CLASSIFICATION_TAG,
            detail_loader=lambda _subject_id: ("Galgame",),
        )
    assert {item.subject_id for item in stored.candidates} == {game.subject_id}


def test_local_edit_is_offline_and_v2_plan_keeps_conflicting_field_out(
    tmp_path: Path,
) -> None:
    database_url = _database(tmp_path)
    base = make_remote_collection(rate=8, comment="base")
    with session_scope(database_url) as session:
        pull_collections(session, [base])
    with session_scope(database_url) as session:
        edit = edit_local_collection(
            session,
            base.subject_id,
            CollectionPatch({"rate": 9, "comment": "local"}),
        )
        assert edit.changed_fields == ("rate", "comment")

    remote = replace(base, comment="remote")
    with session_scope(database_url) as session:
        stored = create_sync_plan(
            session,
            [remote],
            selector={"mode": "ids", "ids": [base.subject_id]},
            fields=("rate", "comment"),
        )
        candidate = stored.planned[0]
        assert stored.plan.format_version == 2
        assert candidate.changed_fields == ("rate",)
        assert candidate.action["values"] == {"rate": 9}
        assert candidate.selection_evidence["field_statuses"]["comment"] == "conflict"


def test_v2_apply_patches_only_selected_field_and_reverse_restores_it(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    remote = make_remote_collection(rate=8, comment="untouched", private=True)
    with session_scope(database_url) as session:
        pull_collections(session, [remote])
        edit_local_collection(session, remote.subject_id, CollectionPatch({"rate": 9}))
    with session_scope(database_url) as session:
        stored = create_sync_plan(
            session,
            [remote],
            selector={"mode": "ids", "ids": [remote.subject_id]},
            fields=("rate",),
        )
        plan_id = stored.plan.id
        review_plan(session, plan_id)

    client = _GenericClient(remote)
    preflight = preflight_plan(database_url, client, plan_id)  # type: ignore[arg-type]
    result = apply_reviewed_plan(
        database_url,
        client,  # type: ignore[arg-type]
        plan_id,
        preflight,
        backup_directory=tmp_path / "backups",
        write_delay_seconds=0,
        max_retries=0,
        retry_base_seconds=0.01,
        sleep_fn=lambda _seconds: None,
    )
    assert client.payloads == [{"rate": 9}]
    assert client.remote.comment == "untouched" and client.remote.private is True
    assert result.reverse_plan_id is not None
    with session_scope(database_url) as session:
        assert local_snapshot(session, remote.subject_id).rating == 9
        operation = session.scalar(select(RemoteOperation))
        assert operation is not None and operation.request_payload_json == '{"rate":9}'
        reverse = load_plan(session, result.reverse_plan_id)
        assert reverse.plan.format_version == 2
        assert reverse.planned[0].action["values"] == {"rate": 8}

    with session_scope(database_url) as session:
        review_plan(session, result.reverse_plan_id)
    reverse_preflight = preflight_plan(
        database_url, client, result.reverse_plan_id  # type: ignore[arg-type]
    )
    apply_reviewed_plan(
        database_url,
        client,  # type: ignore[arg-type]
        result.reverse_plan_id,
        reverse_preflight,
        backup_directory=tmp_path / "backups",
        write_delay_seconds=0,
        max_retries=0,
        retry_base_seconds=0.01,
        sleep_fn=lambda _seconds: None,
    )
    assert client.payloads == [{"rate": 9}, {"rate": 8}]
    assert client.remote.rate == 8


def test_v2_preflight_rejects_stale_local(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    remote = make_remote_collection(rate=8)
    with session_scope(database_url) as session:
        pull_collections(session, [remote])
        edit_local_collection(session, remote.subject_id, CollectionPatch({"rate": 9}))
    with session_scope(database_url) as session:
        stored = create_sync_plan(
            session,
            [remote],
            selector={"mode": "ids", "ids": [remote.subject_id]},
            fields=("rate",),
        )
        plan_id = stored.plan.id
        review_plan(session, plan_id)
    with session_scope(database_url) as session:
        edit_local_collection(session, remote.subject_id, CollectionPatch({"rate": 10}))

    preflight = preflight_plan(
        database_url, _GenericClient(remote), plan_id  # type: ignore[arg-type]
    )
    assert preflight.will_modify == ()
    assert preflight.unchanged[0].reason == "stale_local"


def test_collection_patch_rejects_invalid_or_empty_values() -> None:
    for values in (
        {},
        {"rate": 11},
        {"rate": True},
        {"type": 6},
        {"type": False},
        {"unknown": True},
    ):
        try:
            CollectionPatch(values)
        except MutationValidationError:
            pass
        else:
            raise AssertionError(f"invalid patch was accepted: {values}")


@pytest.mark.parametrize(
    "values",
    (
        {"type": 3},
        {"rate": 10},
        {"comment": "changed"},
        {"private": True},
        {"tags": ("Action", "Galgame分类")},
        {
            "type": 3,
            "rate": 10,
            "comment": "multi",
            "private": True,
            "tags": ("Action", "Galgame分类"),
        },
    ),
    ids=("type", "rate", "comment", "private", "tags", "multi"),
)
def test_v2_apply_supports_each_allowed_field_and_multi_field_payload(
    tmp_path: Path, values: dict[str, object]
) -> None:
    database_url = _database(tmp_path, "-".join(values) + ".sqlite3")
    remote = make_remote_collection(rate=8, tags=("RPG",))
    patch = CollectionPatch(values)
    with session_scope(database_url) as session:
        pull_collections(session, [remote])
        edit_local_collection(session, remote.subject_id, patch)
    with session_scope(database_url) as session:
        stored = create_sync_plan(
            session,
            [remote],
            selector={"mode": "ids", "ids": [remote.subject_id]},
            fields=patch.fields,
        )
        review_plan(session, stored.plan.id)
        plan_id = stored.plan.id

    client = _GenericClient(remote)
    preflight = preflight_plan(database_url, client, plan_id)  # type: ignore[arg-type]
    result = apply_reviewed_plan(
        database_url,
        client,  # type: ignore[arg-type]
        plan_id,
        preflight,
        backup_directory=tmp_path / ("backups-" + "-".join(values)),
        write_delay_seconds=0,
        max_retries=0,
        retry_base_seconds=0.01,
        sleep_fn=lambda _seconds: None,
    )

    assert result.status == "applied"
    assert client.payloads == [patch.as_api_payload()]

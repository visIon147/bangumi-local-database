from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select

from bangumi_local.db.models import (
    Base,
    ChangePlan,
    ChangePlanItem,
    GameProfile,
    LibraryCollection,
    LibraryEntry,
    LibraryEntryCollection,
    PlanApplyRun,
    RemoteOperation,
    SourceAccount,
    Work,
    Tag,
    WorkTag,
)
from bangumi_local.db.repositories import utc_now_iso
from bangumi_local.db.session import session_scope
from bangumi_local.domain.models import SubjectType
from bangumi_local.domain.mutations import CollectionPatch
from bangumi_local.services.discovery import (
    bangumi_discovery_seeds,
    create_discovery_session,
)
from bangumi_local.services.edit_collection import edit_local_collection
from bangumi_local.services.plans import create_sync_plan
from bangumi_local.services.pull import pull_collections
from bangumi_local.services.rating_queue import create_rating_queue, prepare_rating_queue
from bangumi_local.services.read_models import (
    ReadModelError,
    dashboard,
    get_plan_detail,
    get_work_detail,
    list_apply_runs,
    list_discovery_session_summaries,
    list_plans,
    list_rating_queue_summaries,
    list_remote_operations,
    list_works,
    personal_tag_facets,
    steam_summary,
)
from conftest import make_remote_collection


def _database(tmp_path: Path) -> str:
    url = f"sqlite:///{(tmp_path / 'read-models.sqlite3').as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def _seed(database_url: str) -> tuple[int, int, str, str, int]:
    game = make_remote_collection(101, rate=0, tags=("RPG",))
    anime = replace(
        make_remote_collection(102, rate=7, tags=("动画",)),
        subject_type=SubjectType.ANIME,
    )
    now = utc_now_iso()
    with session_scope(database_url) as session:
        pull_collections(session, [game, anime])
        game_work = session.scalar(select(Work).where(Work.bgm_subject_id == 101))
        anime_work = session.scalar(select(Work).where(Work.bgm_subject_id == 102))
        assert game_work is not None and anime_work is not None
        profile = session.get(GameProfile, game_work.id)
        assert profile is not None
        profile.notes_private = "do-not-return-this-note"
        account = SourceAccount(
            source="steam",
            external_account_id="sensitive-account-id",
            account_name="private account",
            config_json='{"machine_path":"D:/private"}',
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(account)
        session.flush()
        entry = LibraryEntry(
            source_account_id=account.id,
            external_id="70",
            work_id=game_work.id,
            title_observed="Half-Life",
            localized_titles_json="{}",
            ownership_scope="installed",
            installed=True,
            playtime_minutes=600,
            metadata_json="{}",
            source_hash="a" * 64,
            match_status="confirmed",
            first_seen_at=now,
            last_seen_at=now,
        )
        collection = LibraryCollection(
            source_account_id=account.id,
            external_id="uc-finished",
            name="完结",
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
        seeds = prepare_rating_queue(
            session,
            subject_types=(4,),
            collection_statuses=(2,),
            include_deferred=False,
            order_name="title",
            random_seed=None,
            max_items=None,
        )
        rating = create_rating_queue(
            session,
            seeds,
            selector={"subject_types": ["game"]},
            order_name="title",
            random_seed=None,
        )
        edit_local_collection(session, 101, CollectionPatch({"rate": 9}))
        plan = create_sync_plan(
            session,
            [game, anime],
            selector={"mode": "ids", "ids": [101]},
            fields=("rate",),
        )
        plan_item = session.scalar(
            select(ChangePlanItem).where(ChangePlanItem.plan_id == plan.plan.id)
        )
        assert plan_item is not None
        run = PlanApplyRun(
            id="run-1",
            plan_id=plan.plan.id,
            status="applied",
            backup_path="D:/private/backups/real.sqlite3",
            started_at=now,
            finished_at=now,
        )
        session.add(run)
        session.flush()
        session.add(
            RemoteOperation(
                id="operation-1",
                run_id=run.id,
                plan_id=plan.plan.id,
                plan_item_id=plan_item.id,
                work_id=game_work.id,
                subject_id=101,
                source="sync",
                request_method="PATCH",
                request_payload_json='{"rate":9}',
                status="applied",
                attempt_count=1,
                http_status=200,
                started_at=now,
                finished_at=now,
            )
        )
        discovery_seeds = bangumi_discovery_seeds(
            session,
            (),
            provider="bangumi_search",
            include_decided=False,
        )
        discovery = create_discovery_session(
            session,
            provider="bangumi_search",
            filters={"query": "safe"},
            seeds=discovery_seeds,
        )
        return game_work.id, anime_work.id, plan.plan.id, rating.session.id, entry.id


def test_dashboard_and_unified_work_pages_are_stable_and_read_only(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    game_work_id, _anime_work_id, _plan_id, _rating_id, _entry_id = _seed(database_url)
    with session_scope(database_url) as session:
        before = session.scalar(select(func.count()).select_from(Work))
        first = list_works(session, page=1, page_size=1)
        second = list_works(session, page=2, page_size=1)
        steam = list_works(session, source="steam", tags=("RPG",))
        view = dashboard(session)
        assert not session.new and not session.dirty and not session.deleted
        after = session.scalar(select(func.count()).select_from(Work))

    assert before == after == 2
    assert first.total == 2 and first.page_count == 2
    assert first.items[0].work_id != second.items[0].work_id
    assert [item.work_id for item in steam.items] == [game_work_id]
    assert steam.items[0].steam_app_ids == ("70",)
    assert steam.items[0].cover_url is not None
    assert view.work_count == 2
    assert view.steam_entry_count == 1
    assert view.actionable_plan_count == 1
    with pytest.raises(FrozenInstanceError):
        first.items[0].title = "mutated"  # type: ignore[misc]
    with pytest.raises(ReadModelError):
        list_works(session, page_size=201)


def test_work_filters_use_exact_bangumi_personal_tags(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    game = replace(make_remote_collection(201, tags=("RPG", "Action")), subject_type=SubjectType.GAME)
    anime = replace(make_remote_collection(202, tags=("RPG", "动画")), subject_type=SubjectType.ANIME)
    with session_scope(database_url) as session:
        pull_collections(session, [game, anime])
        game_work = session.scalar(select(Work).where(Work.bgm_subject_id == 201))
        assert game_work is not None
        local = Tag(name="local-only", sync_scope="local")
        session.add(local)
        session.flush()
        session.add(WorkTag(work_id=game_work.id, tag_id=local.id, origin="agent"))

    with session_scope(database_url) as session:
        include_all = list_works(
            session, tags=("RPG", "Action"), tag_match="all"
        )
        include_any = list_works(
            session, tags=("Action", "动画"), tag_match="any"
        )
        case_mismatch = list_works(session, tags=("rpg",))
        excluded = list_works(session, tags=("RPG",), exclude_tags=("Action",))
        local_only = list_works(session, tags=("local-only",))
        facets = personal_tag_facets(session)

    assert [item.subject_id for item in include_all.items] == [201]
    assert {item.subject_id for item in include_any.items} == {201, 202}
    assert case_mismatch.total == 0
    assert [item.subject_id for item in excluded.items] == [202]
    assert local_only.total == 0
    assert "local-only" not in {item.name for item in facets}
    assert include_all.items[0].tags == ("Action", "RPG")


def test_work_detail_is_detached_and_hides_machine_and_private_note_values(
    tmp_path: Path,
) -> None:
    database_url = _database(tmp_path)
    game_work_id, _anime_work_id, _plan_id, _rating_id, _entry_id = _seed(database_url)
    with session_scope(database_url) as session:
        detail = get_work_detail(session, game_work_id)
    assert detail.bangumi is not None and detail.bangumi.subject_id == 101
    assert detail.game_profile is not None and detail.game_profile.has_private_notes
    assert not hasattr(detail.game_profile, "notes_private")
    assert detail.steam_entries[0].app_id == "70"
    rendered = repr(detail)
    assert "sensitive-account-id" not in rendered
    assert "D:/private" not in rendered
    assert "do-not-return-this-note" not in rendered


def test_plan_and_audit_read_models_verify_content_and_hide_backup_path(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    _game_work_id, _anime_work_id, plan_id, _rating_id, _entry_id = _seed(database_url)
    with session_scope(database_url) as session:
        plans = list_plans(session, statuses=("draft",))
        detail = get_plan_detail(session, plan_id)
        runs = list_apply_runs(session, plan_id=plan_id)
        operations = list_remote_operations(session, run_id="run-1")

    assert plans.total == 1 and plans.items[0].planned_count == 1
    assert detail.summary.plan_id == plan_id
    assert detail.items[0].changed_fields == ("rate",)
    assert runs.items[0].backup_created is True
    assert not hasattr(runs.items[0], "backup_path")
    assert operations.items[0].request_payload_json == '{"rate":9}'
    assert operations.items[0].http_status == 200


def test_source_and_queue_summaries_do_not_return_orm_or_private_reasons(
    tmp_path: Path,
) -> None:
    database_url = _database(tmp_path)
    _game_work_id, _anime_work_id, _plan_id, rating_id, _entry_id = _seed(database_url)
    with session_scope(database_url) as session:
        steam = steam_summary(session)
        ratings = list_rating_queue_summaries(session)
        discoveries = list_discovery_session_summaries(session)

    assert steam.entry_count == 1 and steam.matched_count == 1
    assert ratings.items[0].session_id == rating_id
    assert ratings.items[0].pending_count == 1
    assert not hasattr(ratings.items[0], "selector_json")
    assert discoveries.total == 1
    assert discoveries.items[0].provider == "bangumi_search"
    assert not hasattr(discoveries.items[0], "filters_json")

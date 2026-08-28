from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from bangumi_local.adapters.bangumi import BangumiAPIError
from bangumi_local.adapters.steam import SteamDataError, read_steam_snapshot
from bangumi_local.config import Settings
from bangumi_local.db.models import (
    BangumiCollectionState,
    Base,
    ChangePlan,
    LibraryEntry,
    LibraryEntryCollection,
    LibraryMatchReview,
    MediaSource,
    SourceAccount,
    SyncShadow,
    WorkLink,
)
from bangumi_local.db.session import session_scope
from bangumi_local.domain.models import (
    CollectionStatus,
    RemoteCollection,
    RemoteSubject,
    SubjectSearchCandidate,
    SubjectType,
)
from bangumi_local.domain.mutations import CollectionPatch
from bangumi_local.domain.steam import (
    SteamRuleConfiguration,
    SteamStatusRule,
    classify_collections,
)
from bangumi_local.services.apply_plan import apply_reviewed_plan, preflight_plan
from bangumi_local.services.plans import load_plan, review_plan
from bangumi_local.services.plans import PlanError
from bangumi_local.services.manual_uncollect import reconcile_manual_uncollect
from bangumi_local.services.steam_import import apply_steam_import, preview_steam_import
from bangumi_local.services.steam_matching import (
    confirm_match,
    normalize_title,
    search_matches,
    set_match_disposition,
)
from bangumi_local.services.steam_match_apply import (
    apply_steam_match_plan,
    preflight_steam_match_plan,
)
from bangumi_local.services.steam_match_plans import (
    AutoMatchPolicy,
    create_steam_match_plan,
    fetch_steam_match_plan,
    persist_steam_match_plan,
    prepare_steam_match_plan,
    revise_steam_match_plan,
)
from bangumi_local.services.steam_match_media import register_match_candidate_media
from bangumi_local.services.steam_plans import create_steam_status_plan
from bangumi_local.services.steam_titles import (
    SteamTitleError,
    fetch_title_completion,
    persist_title_completion,
    prepare_title_completion,
    set_manual_title,
)


def _steam_root(tmp_path: Path) -> Path:
    root = tmp_path / "Steam Root"
    account = root / "userdata/123/config"
    (account / "cloudstorage").mkdir(parents=True)
    value = {
        "id": "uc-complete",
        "name": "故事完结",
        "added": [100, 200],
        "removed": [200],
    }
    cloud = [["user-collections.uc-complete", {"value": json.dumps(value)}]]
    (account / "cloudstorage/cloud-storage-namespace-1.json").write_text(
        json.dumps(cloud, ensure_ascii=False), encoding="utf-8"
    )
    (account / "localconfig.vdf").write_text(
        '"UserLocalConfigStore" { "Software" { "Valve" { "Steam" { "apps" '
        '{ "100" { "name" "Example Game" "Playtime" "42" } } } } } }',
        encoding="utf-8",
    )
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders" {{ "0" {{ "path" "{root.as_posix()}" }} }}',
        encoding="utf-8",
    )
    return root


def _settings(root: Path) -> Settings:
    return Settings(_env_file=None, steam_root=root, steam_account_id="123")


def _database(tmp_path: Path) -> str:
    path = tmp_path / "phase5.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def test_cloud_import_is_local_only_idempotent_and_does_not_store_paths(tmp_path: Path) -> None:
    settings = _settings(_steam_root(tmp_path))
    snapshot = read_steam_snapshot(settings)
    assert snapshot.source_kind == "steam_cloudstorage"
    assert [item.app_ids for item in snapshot.collections] == [("100",)]
    entry = next(item for item in snapshot.entries if item.app_id == "100")
    assert entry.title == "Example Game"
    assert entry.playtime_minutes == 42

    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        dry_run = preview_steam_import(session, snapshot)
        assert dry_run.new_entries == 1 and not dry_run.applied
    with session_scope(database_url) as session:
        applied = apply_steam_import(session, snapshot)
        assert applied.applied and applied.new_entries == 1
        assert applied.membership_changes == 1
    with session_scope(database_url) as session:
        repeated = apply_steam_import(session, snapshot)
        assert repeated.new_entries == 0 and repeated.updated_entries == 0
        assert repeated.membership_changes == 0
        account = session.scalar(select(SourceAccount))
        assert account is not None
        assert str(settings.steam_root) not in account.config_json


def test_corrupt_cloud_cache_falls_back_to_legacy_categories(tmp_path: Path) -> None:
    root = _steam_root(tmp_path)
    account_root = root / "userdata/123"
    (account_root / "config/cloudstorage/cloud-storage-namespace-1.json").write_text(
        "{broken", encoding="utf-8"
    )
    legacy = account_root / "7/remote"
    legacy.mkdir(parents=True)
    (legacy / "sharedconfig.vdf").write_text(
        '"UserRoamingConfigStore" { "Software" { "Valve" { "Steam" { "Apps" '
        '{ "100" { "tags" { "0" "在用" } } } } } } }',
        encoding="utf-8",
    )

    snapshot = read_steam_snapshot(_settings(root))

    assert snapshot.source_kind == "steam_sharedconfig"
    assert snapshot.collections[0].name == "在用"
    assert snapshot.collections[0].app_ids == ("100",)


def test_single_account_is_required_when_not_configured(tmp_path: Path) -> None:
    root = _steam_root(tmp_path)
    (root / "userdata/456").mkdir()
    settings = Settings(_env_file=None, steam_root=root)

    with pytest.raises(SteamDataError, match="Multiple Steam accounts"):
        read_steam_snapshot(settings)


def test_complete_snapshot_deactivates_removed_membership_but_partial_does_not(
    tmp_path: Path,
) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)

    empty_entry = replace(snapshot.entries[0], collection_ids=())
    partial = replace(
        snapshot,
        entries=(empty_entry,),
        category_snapshot_complete=False,
    )
    with session_scope(database_url) as session:
        result = apply_steam_import(session, partial)
        membership = session.scalar(select(LibraryEntryCollection))
        assert result.membership_changes == 0
        assert membership is not None and membership.active

    complete = replace(partial, category_snapshot_complete=True)
    with session_scope(database_url) as session:
        result = apply_steam_import(session, complete)
        membership = session.scalar(select(LibraryEntryCollection))
        assert result.membership_changes == 1
        assert membership is not None and not membership.active
    with session_scope(database_url) as session:
        assert apply_steam_import(session, complete).membership_changes == 0


def test_status_rules_support_exact_contains_regex_and_conflict() -> None:
    configuration = SteamRuleConfiguration(
        rules=(
            SteamStatusRule("contains", "完结", CollectionStatus.DONE),
            SteamStatusRule("exact", "在用", CollectionStatus.DOING),
            SteamStatusRule("regex", r"^Later$", CollectionStatus.ON_HOLD, False),
        )
    )
    assert classify_collections(("故事完结",), configuration)[0] == CollectionStatus.DONE
    assert classify_collections(("在用",), configuration)[0] == CollectionStatus.DOING
    assert classify_collections(("later",), configuration)[0] == CollectionStatus.ON_HOLD
    desired, reasons, conflict = classify_collections(("完结", "在用"), configuration)
    assert desired is None and conflict and len(reasons) == 2


def test_title_normalization_removes_trademark_before_nfkc_expansion() -> None:
    assert normalize_title("Middle-earth™: Shadow of Mordor™") == (
        "middle earth shadow of mordor"
    )


class _MatchClient:
    candidate = SubjectSearchCandidate(
        subject_id=900,
        subject_type=SubjectType.GAME,
        title_original="Example Game",
        title_cn="示例游戏",
        summary="summary",
        release_date="2024-01-01",
        cover_url="https://lain.bgm.tv/pic/cover/common/example.jpg",
        aliases=("Example",),
        public_tags=("Game",),
    )

    def search_subjects(self, *_args: object, **_kwargs: object) -> list[SubjectSearchCandidate]:
        return [self.candidate]

    def get_subject(self, subject_id: int) -> SubjectSearchCandidate:
        assert subject_id == self.candidate.subject_id
        return self.candidate


class _NumberedMatchClient:
    def search_subjects(self, query: str, **_kwargs: object) -> list[SubjectSearchCandidate]:
        number = int(query.casefold().removeprefix("game "))
        return [
            SubjectSearchCandidate(
                subject_id=100_000 + number,
                subject_type=SubjectType.GAME,
                title_original=f"Game {number}",
                title_cn=None,
                summary=None,
                release_date=None,
                cover_url=None,
            )
        ]


def test_batch_match_keeps_229_entries_in_one_immutable_plan(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    now = "2026-08-28T00:00:00Z"
    with session_scope(database_url) as session:
        account = SourceAccount(
            source="steam", external_account_id="123", config_json="{}",
            first_seen_at=now, last_seen_at=now,
        )
        session.add(account)
        session.flush()
        for number in range(1, 230):
            session.add(
                LibraryEntry(
                    source_account_id=account.id, external_id=str(number),
                    title_observed=f"Game {number}", localized_titles_json="{}",
                    ownership_scope="visible", metadata_json="{}",
                    source_hash=f"hash-{number}", match_status="unmatched",
                    first_seen_at=now, last_seen_at=now,
                )
            )
        session.flush()
        prepared = prepare_steam_match_plan(
            session, account_id="123", all_unmatched=True, max_items=250,
            candidate_limit=1,
        )
        assert len(prepared.entries) == 229
    fetched = fetch_steam_match_plan(
        prepared, _NumberedMatchClient(),  # type: ignore[arg-type]
        include_store_titles=False, timeout_seconds=1,
        request_delay_seconds=0, sleep_fn=lambda _seconds: None,
    )
    with session_scope(database_url) as session:
        stored = persist_steam_match_plan(session, fetched)
        assert len(stored.candidates) == 229
        assert len(json.loads(stored.plan.selector_json)["evaluated_app_ids"]) == 229


def test_batch_match_continue_records_auth_circuit_and_progress(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    now = "2026-08-28T00:00:00Z"
    with session_scope(database_url) as session:
        account = SourceAccount(
            source="steam", external_account_id="123", config_json="{}",
            first_seen_at=now, last_seen_at=now,
        )
        session.add(account)
        session.flush()
        for number in range(1, 6):
            session.add(
                LibraryEntry(
                    source_account_id=account.id, external_id=str(number),
                    title_observed=f"Game {number}", localized_titles_json="{}",
                    ownership_scope="visible", metadata_json="{}",
                    source_hash=f"hash-{number}", match_status="unmatched",
                    first_seen_at=now, last_seen_at=now,
                )
            )
        session.flush()
        prepared = prepare_steam_match_plan(
            session, account_id="123", all_unmatched=True,
            failure_policy="continue", candidate_limit=1,
        )

    class AuthClient:
        calls = 0

        def search_subjects(self, *_args: object, **_kwargs: object):
            self.calls += 1
            raise BangumiAPIError("unauthorized", status_code=401)

    client = AuthClient()
    progress: list[tuple[int, int]] = []
    fetched = fetch_steam_match_plan(
        prepared, client,  # type: ignore[arg-type]
        include_store_titles=False, timeout_seconds=1, request_delay_seconds=0,
        max_retries=0, progress_fn=lambda current, total, _message: progress.append((current, total)),
        sleep_fn=lambda _seconds: None,
    )
    assert client.calls == 3
    assert [item.failure_code for item in fetched.entries] == [
        "steam_match_auth_failed", "steam_match_auth_failed", "steam_match_auth_failed",
        "steam_match_auth_unavailable", "steam_match_auth_unavailable",
    ]
    assert progress[-1] == (5, 5)


def test_batch_match_fail_fast_auth_does_not_retry(tmp_path: Path) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
        prepared = prepare_steam_match_plan(
            session, account_id="123", app_ids=("100",), failure_policy="fail_fast"
        )

    class AuthClient:
        calls = 0

        def search_subjects(self, *_args: object, **_kwargs: object):
            self.calls += 1
            raise BangumiAPIError("unauthorized", status_code=403)

    client = AuthClient()
    with pytest.raises(BangumiAPIError):
        fetch_steam_match_plan(
            prepared, client,  # type: ignore[arg-type]
            include_store_titles=False, timeout_seconds=1,
            request_delay_seconds=0, max_retries=4,
            sleep_fn=lambda _seconds: None,
        )
    assert client.calls == 1


def test_match_candidates_never_auto_confirm_and_manual_decisions_are_audited(
    tmp_path: Path,
) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
    with session_scope(database_url) as session:
        result = search_matches(
            session,
            _MatchClient(),  # type: ignore[arg-type]
            app_id="100",
            account_id="123",
            query=None,
            include_store_titles=False,
            timeout_seconds=1,
        )
        entry = session.scalar(select(LibraryEntry).where(LibraryEntry.external_id == "100"))
        assert result.candidates[0].subject_id == 900
        assert entry is not None and entry.match_status == "candidates" and entry.work_id is None
    with session_scope(database_url) as session:
        entry, work = confirm_match(
            session,
            _MatchClient(),  # type: ignore[arg-type]
            app_id="100",
            subject_id=900,
            account_id="123",
        )
        assert entry.match_status == "confirmed" and entry.work_id == work.id
        link = session.scalar(
            select(WorkLink).where(WorkLink.source == "steam", WorkLink.external_id == "100")
        )
        assert link is not None and link.match_confidence == "confirmed"
    with session_scope(database_url) as session:
        entry = session.scalar(select(LibraryEntry).where(LibraryEntry.external_id == "100"))
        assert entry is not None
        try:
            set_match_disposition(
                session,
                app_id="100",
                account_id="123",
                decision="no_subject",
                reason=None,
            )
        except Exception as exc:
            assert "confirmed" in str(exc)
        else:
            raise AssertionError("confirmed mapping was unexpectedly cleared")


def test_v4_batch_match_auto_confirms_only_after_plan_review_and_local_apply(
    tmp_path: Path,
) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
        stored = create_steam_match_plan(
            session,
            _MatchClient(),  # type: ignore[arg-type]
            account_id="123",
            app_ids=("100",),
            policy=AutoMatchPolicy(score_threshold=95, minimum_margin=20),
            include_store_titles=False,
        )
        assert stored.plan.format_version == 4
        assert len(stored.planned) == 1
        item = stored.planned[0]
        assert item.reason == "steam_match_auto_confirm"
        assert item.subject_id == 900
        assert item.action["confirmation"] == "automatic"
        assert item.selection_evidence["selected_score"] == 100
        candidate = item.selection_evidence["match_candidates"][0]
        assert candidate["summary"] == "summary"
        assert candidate["public_tags"] == ["Game"]
        assert candidate["cover_url"].endswith("example.jpg")
        assert candidate["url"].endswith("/900")
        media = register_match_candidate_media(session, stored, policy="cache")
        assert media.observed == 1 and len(media.missing) == 1
        source = session.scalar(select(MediaSource))
        assert source is not None and source.current_blob_sha256 is None
        selector = json.loads(stored.plan.selector_json)
        assert selector["candidate_image_policy"] == "metadata"
        entry = session.scalar(select(LibraryEntry))
        assert entry is not None and entry.match_status == "candidates"
        review_plan(session, stored.plan.id)
        plan_id = stored.plan.id

    preflight = preflight_steam_match_plan(
        database_url, _MatchClient(), plan_id  # type: ignore[arg-type]
    )
    assert len(preflight.will_modify) == 1 and not preflight.unchanged
    result = apply_steam_match_plan(
        database_url,
        _MatchClient(),  # type: ignore[arg-type]
        plan_id,
        preflight,
        backup_directory=tmp_path / "backups",
    )
    assert result.status == "applied" and result.applied == 1
    assert result.reverse_plan_id is None and result.backup_path.is_file()
    with session_scope(database_url) as session:
        entry = session.scalar(select(LibraryEntry))
        review = session.scalar(select(LibraryMatchReview))
        link = session.scalar(
            select(WorkLink).where(WorkLink.source == "steam", WorkLink.external_id == "100")
        )
        assert entry is not None and entry.match_status == "confirmed"
        assert review is not None and review.plan_id == plan_id and review.score == 100
        assert review.reason == "automatic_threshold_confirmation"
        assert link is not None and link.match_source == "automatic_match_plan"
        assert link.match_confidence == "high"


class _LowMatchClient(_MatchClient):
    candidate = SubjectSearchCandidate(
        subject_id=901,
        subject_type=SubjectType.GAME,
        title_original="Completely Different Adventure",
        title_cn="完全不同的冒险",
        summary="summary",
        release_date="2020-01-01",
        cover_url=None,
        aliases=(),
        public_tags=("Game",),
    )


class _RevisionClient(_MatchClient):
    alternate = SubjectSearchCandidate(
        subject_id=902,
        subject_type=SubjectType.GAME,
        title_original="Manually Selected Game",
        title_cn="人工选择游戏",
        summary="summary",
        release_date="2024-01-01",
        cover_url=None,
        aliases=(),
        public_tags=("Game",),
    )

    def get_subject(self, subject_id: int) -> SubjectSearchCandidate:
        if subject_id == self.alternate.subject_id:
            return self.alternate
        return super().get_subject(subject_id)


def test_v4_low_score_requires_review_and_revision_creates_successor_draft(
    tmp_path: Path,
) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
        original = create_steam_match_plan(
            session,
            _LowMatchClient(),  # type: ignore[arg-type]
            account_id="123",
            app_ids=("100",),
            include_store_titles=False,
        )
        assert not original.planned
        assert original.unchanged[0].reason == "steam_match_manual_review"
        successor = revise_steam_match_plan(
            session,
            _RevisionClient(),  # type: ignore[arg-type]
            plan_id=original.plan.id,
            app_id="100",
            decision="subject",
            subject_id=902,
        )
        assert session.get(ChangePlan, original.plan.id).status == "cancelled"  # type: ignore[union-attr]
        assert successor.planned[0].subject_id == 902
        assert successor.planned[0].reason == "steam_match_manual_override"
        assert successor.planned[0].action["confirmation"] == "manual_override"
        assert successor.plan.content_hash != original.plan.content_hash


def test_v4_no_subject_revision_is_audited_local_only(tmp_path: Path) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
        original = create_steam_match_plan(
            session,
            _LowMatchClient(),  # type: ignore[arg-type]
            account_id="123",
            app_ids=("100",),
            include_store_titles=False,
        )
        successor = revise_steam_match_plan(
            session,
            None,
            plan_id=original.plan.id,
            app_id="100",
            decision="no_subject",
        )
        assert successor.planned[0].action["operation"] == "set_no_subject"
        review_plan(session, successor.plan.id)
        plan_id = successor.plan.id
    preflight = preflight_steam_match_plan(
        database_url, _LowMatchClient(), plan_id  # type: ignore[arg-type]
    )
    result = apply_steam_match_plan(
        database_url,
        _LowMatchClient(),  # type: ignore[arg-type]
        plan_id,
        preflight,
        backup_directory=tmp_path / "backups",
    )
    assert result.status == "applied"
    with session_scope(database_url) as session:
        entry = session.scalar(select(LibraryEntry))
        review = session.scalar(select(LibraryMatchReview))
        assert entry is not None and entry.match_status == "no_subject"
        assert review is not None and review.plan_id == plan_id
        assert review.reason == "manual_plan_no_subject"


def test_v4_batch_duplicate_subjects_are_never_automatically_confirmed(
    tmp_path: Path,
) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
        account = session.scalar(select(SourceAccount))
        assert account is not None
        now = "2026-08-27T00:00:00Z"
        session.add(
            LibraryEntry(
                source_account_id=account.id,
                external_id="200",
                title_observed="Example",
                localized_titles_json="{}",
                ownership_scope="visible",
                metadata_json="{}",
                source_hash="second-entry-hash",
                match_status="unmatched",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        session.flush()
        stored = create_steam_match_plan(
            session,
            _MatchClient(),  # type: ignore[arg-type]
            account_id="123",
            app_ids=("100", "200"),
            include_store_titles=False,
        )
        assert not stored.planned
        assert len(stored.unchanged) == 2
        assert {item.reason for item in stored.unchanged} == {"steam_match_batch_collision"}
        assert all(item.subject_id is None for item in stored.unchanged)


def test_v4_duplicate_normalized_source_titles_require_manual_merge(
    tmp_path: Path,
) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
        account = session.scalar(select(SourceAccount))
        assert account is not None
        now = "2026-08-27T00:00:00Z"
        session.add(
            LibraryEntry(
                source_account_id=account.id,
                external_id="200",
                title_observed="Example Game Edition",
                localized_titles_json="{}",
                ownership_scope="visible",
                metadata_json="{}",
                source_hash="edition-entry-hash",
                match_status="unmatched",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        session.flush()
        stored = create_steam_match_plan(
            session,
            _MatchClient(),  # type: ignore[arg-type]
            account_id="123",
            app_ids=("100",),
            include_store_titles=False,
        )
        assert not stored.planned
        assert stored.unchanged[0].reason == "steam_match_source_title_collision"
        assert stored.unchanged[0].selection_evidence["source_title_collisions"] == ["200"]


def test_v4_match_preflight_excludes_stale_steam_source(tmp_path: Path) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
        stored = create_steam_match_plan(
            session,
            _MatchClient(),  # type: ignore[arg-type]
            account_id="123",
            app_ids=("100",),
            include_store_titles=False,
        )
        review_plan(session, stored.plan.id)
        plan_id = stored.plan.id
        entry = session.scalar(select(LibraryEntry))
        assert entry is not None
        entry.source_hash = "changed-after-plan"
    preflight = preflight_steam_match_plan(
        database_url, _MatchClient(), plan_id  # type: ignore[arg-type]
    )
    assert not preflight.will_modify
    assert preflight.unchanged[0].reason == "stale_source"


def test_v4_missing_title_is_listed_for_manual_input_instead_of_aborting(
    tmp_path: Path,
) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
        entry = session.scalar(select(LibraryEntry))
        assert entry is not None
        entry.title_observed = None
        entry.localized_titles_json = "{}"
        stored = create_steam_match_plan(
            session,
            _MatchClient(),  # type: ignore[arg-type]
            account_id="123",
            app_ids=("100",),
            include_store_titles=False,
        )
        assert not stored.planned
        assert stored.unchanged[0].reason == "steam_match_title_unavailable"


def test_steam_store_title_completion_and_manual_override_survive_import(
    tmp_path: Path,
) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
        entry = session.scalar(select(LibraryEntry))
        assert entry is not None
        entry.title_observed = None
        entry.localized_titles_json = "{}"
    with session_scope(database_url) as session:
        prepared = prepare_title_completion(
            session,
            account_id="123",
            app_ids=None,
            all_missing=True,
        )
    fetched = fetch_title_completion(
        prepared,
        timeout_seconds=1,
        request_delay_seconds=0,
        fetch_fn=lambda _app_id, **_kwargs: {
            "english": "Example Store Title",
            "schinese": "示例商店标题",
        },
    )
    with session_scope(database_url) as session:
        result = persist_title_completion(session, fetched)
        entry = session.scalar(select(LibraryEntry))
        assert result.updated == 1 and entry is not None
        assert entry.title_observed == "Example Store Title"
        assert json.loads(entry.localized_titles_json)["schinese"] == "示例商店标题"
        set_manual_title(
            session,
            account_id="123",
            app_id="100",
            title="My Manual Title",
        )
    changed_snapshot = replace(
        snapshot,
        entries=(replace(snapshot.entries[0], title="New Imported Title"),),
    )
    with session_scope(database_url) as session:
        apply_steam_import(session, changed_snapshot)
        entry = session.scalar(select(LibraryEntry))
        assert entry is not None and entry.title_observed == "My Manual Title"
        assert json.loads(entry.metadata_json)["manual_title"] == "My Manual Title"


def test_match_all_unresolved_can_explicitly_reinclude_dispositions(
    tmp_path: Path,
) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
        set_match_disposition(
            session,
            app_id="100",
            account_id="123",
            decision="deferred",
            reason="later",
        )
        with pytest.raises(PlanError, match="no entries"):
            create_steam_match_plan(
                session,
                _MatchClient(),  # type: ignore[arg-type]
                account_id="123",
                all_unmatched=True,
                include_store_titles=False,
            )
        stored = create_steam_match_plan(
            session,
            _MatchClient(),  # type: ignore[arg-type]
            account_id="123",
            all_unmatched=True,
            include_deferred=True,
            include_store_titles=False,
        )
        assert len(stored.planned) == 1
        assert stored.planned[0].selection_evidence["match_status"] == "deferred"


def test_title_completion_refuses_implicit_batch_over_250(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    now = "2026-08-28T00:00:00Z"
    with session_scope(database_url) as session:
        account = SourceAccount(
            source="steam",
            external_account_id="123",
            config_json="{}",
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(account)
        session.flush()
        for app_id in range(1, 252):
            session.add(
                LibraryEntry(
                    source_account_id=account.id,
                    external_id=str(app_id),
                    localized_titles_json="{}",
                    ownership_scope="visible",
                    metadata_json="{}",
                    source_hash=f"hash-{app_id}",
                    match_status="unmatched",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        session.flush()
        with pytest.raises(SteamTitleError, match="251 entries"):
            prepare_title_completion(
                session,
                account_id="123",
                app_ids=None,
                all_missing=True,
            )


def test_no_subject_defer_and_reopen_are_explicit_audited_states(tmp_path: Path) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
    with session_scope(database_url) as session:
        set_match_disposition(
            session,
            app_id="100",
            account_id="123",
            decision="deferred",
            reason="ambiguous edition",
        )
        set_match_disposition(
            session,
            app_id="100",
            account_id="123",
            decision="reopened",
            reason="retry",
        )
        set_match_disposition(
            session,
            app_id="100",
            account_id="123",
            decision="no_subject",
            reason="manual search complete",
        )
        entry = session.scalar(select(LibraryEntry))
        reviews = session.scalars(
            select(LibraryMatchReview).order_by(LibraryMatchReview.created_at)
        ).all()
        assert entry is not None and entry.match_status == "no_subject"
        assert [item.decision for item in reviews] == [
            "deferred",
            "reopened",
            "no_subject",
        ]


class _LifecycleClient:
    def __init__(self) -> None:
        self.remote: RemoteCollection | None = None
        self.created = 0

    def get_collection(self, subject_id: int) -> RemoteCollection:
        if self.remote is None:
            raise BangumiAPIError("missing", status_code=404)
        assert self.remote.subject_id == subject_id
        return self.remote

    def create_collection(self, subject_id: int, patch: CollectionPatch) -> None:
        self.created += 1
        self.remote = RemoteCollection(
            subject_id=subject_id,
            subject_type=SubjectType.GAME,
            status=CollectionStatus(int(patch.values["type"])),
            rate=0,
            comment="",
            tags=(),
            updated_at=datetime.now(timezone.utc),
            private=False,
            subject=RemoteSubject(
                subject_id=subject_id,
                title_original="Example Game",
                title_cn=None,
                summary=None,
                release_date=None,
                cover_url=None,
            ),
        )

    def patch_collection(self, _subject_id: int, _patch: CollectionPatch) -> None:
        raise AssertionError("create test should not PATCH")



def test_v3_create_and_manual_uncollect_reconciliation_round_trip(tmp_path: Path) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
    with session_scope(database_url) as session:
        confirm_match(
            session,
            _MatchClient(),  # type: ignore[arg-type]
            app_id="100",
            subject_id=900,
            account_id="123",
        )
    rules = SteamRuleConfiguration(
        rules=(SteamStatusRule("contains", "完结", CollectionStatus.DONE),)
    )
    with session_scope(database_url) as session:
        stored = create_steam_status_plan(
            session,
            [],
            app_ids=("100",),
            all_eligible=False,
            account_id="123",
            configuration=rules,
            remaining_status=None,
            followup_tag=None,
        )
        assert stored.plan.format_version == 3
        assert stored.planned[0].action["operation"] == "create_collection"
        plan_id = stored.plan.id
        review_plan(session, plan_id)

    client = _LifecycleClient()
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
    assert result.status == "applied" and client.created == 1
    assert result.reverse_plan_id is not None
    with session_scope(database_url) as session:
        assert session.get(BangumiCollectionState, 900) is not None
        assert session.get(SyncShadow, 900) is not None
        reverse = load_plan(session, result.reverse_plan_id)
        assert reverse.planned[0].action["operation"] == "manual_uncollect"
        review_plan(session, result.reverse_plan_id)

    reverse_preflight = preflight_plan(
        database_url, client, result.reverse_plan_id  # type: ignore[arg-type]
    )
    with pytest.raises(PlanError, match="no collection DELETE endpoint"):
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
    with pytest.raises(PlanError, match="still collected"):
        reconcile_manual_uncollect(
            database_url,
            client,  # type: ignore[arg-type]
            result.reverse_plan_id,
            backup_directory=tmp_path / "backups",
        )
    client.remote = None
    reconciliation = reconcile_manual_uncollect(
        database_url,
        client,  # type: ignore[arg-type]
        result.reverse_plan_id,
        backup_directory=tmp_path / "backups",
    )
    assert reconciliation.reconciled == 1
    assert reconciliation.backup_path.is_file()
    assert reconciliation.restore_plan_id is not None
    with session_scope(database_url) as session:
        assert session.get(BangumiCollectionState, 900) is None
        assert session.get(SyncShadow, 900) is None
        restore = load_plan(session, reconciliation.restore_plan_id)
        assert restore.planned[0].action["operation"] == "create_collection"
        assert restore.planned[0].intended_snapshot is not None
        assert restore.planned[0].intended_snapshot.collection_type == 2


def test_v3_preflight_rejects_changed_steam_membership(tmp_path: Path) -> None:
    snapshot = read_steam_snapshot(_settings(_steam_root(tmp_path)))
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        apply_steam_import(session, snapshot)
        confirm_match(
            session,
            _MatchClient(),  # type: ignore[arg-type]
            app_id="100",
            subject_id=900,
            account_id="123",
        )
        plan = create_steam_status_plan(
            session,
            [],
            app_ids=("100",),
            all_eligible=False,
            account_id="123",
            configuration=SteamRuleConfiguration(
                rules=(SteamStatusRule("contains", "完结", CollectionStatus.DONE),)
            ),
            remaining_status=None,
            followup_tag=None,
        )
        review_plan(session, plan.plan.id)
        membership = session.scalar(select(LibraryEntryCollection))
        assert membership is not None
        membership.active = False
        plan_id = plan.plan.id
    preflight = preflight_plan(database_url, _LifecycleClient(), plan_id)  # type: ignore[arg-type]
    assert preflight.will_modify == ()
    assert any(item.reason == "stale_source" for item in preflight.unchanged)

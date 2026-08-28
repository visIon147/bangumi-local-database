from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from typer.testing import CliRunner

from bangumi_local.cli import app
from bangumi_local.db.models import (
    Base,
    BangumiCollectionState,
    DiscoveryReviewEvent,
    DiscoveryReviewState,
    DiscoveryCandidate,
    LibraryCollection,
    LibraryEntry,
    LibraryEntryCollection,
    RatingReviewEvent,
    RatingReviewState,
    RatingQueueItem,
    SourceAccount,
)
from bangumi_local.db.session import session_scope
from bangumi_local.domain.models import CollectionStatus, SubjectSearchCandidate, SubjectType
from bangumi_local.services.discovery import (
    bangumi_discovery_seeds,
    create_discovery_session,
    create_failed_discovery_session,
    decide_discovery_candidate,
    promotion_preview,
    reopen_discovery_candidate,
    steam_discovery_seeds,
)
from bangumi_local.services.pull import pull_collections
from bangumi_local.services.rating_queue import (
    RatingQueueError,
    create_rating_queue,
    load_rating_queue,
    next_rating_item,
    prepare_rating_queue,
    rate_rating_item,
    reopen_rating_subject,
    set_rating_disposition,
)
from bangumi_local.services.plans import create_sync_plan
from conftest import make_remote_collection


def _database(tmp_path: Path) -> str:
    path = tmp_path / "queues.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{path.as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def _rating_queue(database_url: str, subject_ids: tuple[int, ...] = (101, 102)) -> str:
    with session_scope(database_url) as session:
        for subject_id in subject_ids:
            pull_collections(
                session,
                [make_remote_collection(subject_id, rate=0, comment="", tags=())],
            )
        seeds = prepare_rating_queue(
            session,
            subject_types=(4,),
            collection_statuses=(2, 3, 4, 5),
            include_deferred=False,
            order_name="title",
            random_seed=None,
            max_items=None,
        )
        view = create_rating_queue(
            session,
            seeds,
            selector={"subject_types": ["game"]},
            order_name="title",
            random_seed=None,
        )
        return view.session.id


def test_rating_queue_is_fixed_and_supports_rate_skip_defer_reopen(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    queue_id = _rating_queue(database_url, (101, 102, 103))
    with session_scope(database_url) as session:
        first = next_rating_item(session, queue_id)
        assert first is not None and first.subject_id == 101
        rate_rating_item(
            session, queue_id, 101, score=9, reason="private",
            skip_reason=False, publish_reason=False, public_comment=None,
            replace_existing_comment=False,
        )
        set_rating_disposition(session, queue_id, 102, decision="skipped", reason=None)
        set_rating_disposition(session, queue_id, 103, decision="deferred", reason="later")
        assert load_rating_queue(session, queue_id).session.status == "completed"
        assert session.get(BangumiCollectionState, 101).rating == 9  # type: ignore[union-attr]
        assert session.get(RatingReviewState, 101).reason_private == "private"  # type: ignore[union-attr]
        assert session.scalar(select(RatingReviewEvent).where(RatingReviewEvent.subject_id == 101))
        reopened = reopen_rating_subject(session, 103, "retry")
        assert reopened.state == "pending"

    with session_scope(database_url) as session:
        pull_collections(
            session, [make_remote_collection(104, rate=0, comment="", tags=())]
        )
        items = prepare_rating_queue(
            session, subject_types=(4,), collection_statuses=(2,),
            include_deferred=False, order_name="title", random_seed=None,
            max_items=None,
        )
        assert {item.subject_id for item in items} == {103, 104}
        original = load_rating_queue(session, queue_id)
        assert [item.subject_id for item in original.items] == [101, 102, 103]


def test_rating_orders_random_seed_and_reopened_rated_item(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    queue_id = _rating_queue(database_url, (111, 112, 113, 114))
    with session_scope(database_url) as session:
        rate_rating_item(
            session, queue_id, 111, score=6, reason=None,
            skip_reason=True, publish_reason=False, public_comment=None,
            replace_existing_comment=False,
        )
        reopen_rating_subject(session, 111, "reconsider")
        for order_name in (
            "recently-updated", "release-date-desc", "title",
            "subject-type", "collection-status",
        ):
            seeds = prepare_rating_queue(
                session, subject_types=(4,), collection_statuses=(2,),
                include_deferred=False, order_name=order_name, random_seed=None,
                max_items=None,
            )
            assert {seed.subject_id for seed in seeds} == {111, 112, 113, 114}
        first = prepare_rating_queue(
            session, subject_types=(4,), collection_statuses=(2,),
            include_deferred=False, order_name="random", random_seed=8675309,
            max_items=3,
        )
        second = prepare_rating_queue(
            session, subject_types=(4,), collection_statuses=(2,),
            include_deferred=False, order_name="random", random_seed=8675309,
            max_items=3,
        )
        assert [seed.subject_id for seed in first] == [seed.subject_id for seed in second]
        assert len(first) == 3


def test_rating_max_items_does_not_initialize_unselected_unrated_rows(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        pull_collections(
            session,
            [make_remote_collection(value, rate=0, comment="", tags=()) for value in range(121, 126)],
        )
        seeds = prepare_rating_queue(
            session, subject_types=(4,), collection_statuses=(2,),
            include_deferred=False, order_name="title", random_seed=None,
            max_items=2,
        )
        assert len(seeds) == 2
        states = session.scalars(select(RatingReviewState)).all()
        assert {state.subject_id for state in states} == {seed.subject_id for seed in seeds}


def test_rating_comment_protection_stale_and_v2_plan(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    queue_id = _rating_queue(database_url, (201,))
    with session_scope(database_url) as session:
        with pytest.raises(RatingQueueError, match="requires --reason"):
            rate_rating_item(
                session, queue_id, 201, score=8, reason=None,
                skip_reason=False, publish_reason=True, public_comment=None,
                replace_existing_comment=False,
            )
        rate_rating_item(
            session, queue_id, 201, score=8, reason="public reason",
            skip_reason=False, publish_reason=True, public_comment=None,
            replace_existing_comment=False,
        )
        remote = make_remote_collection(201, rate=0, comment="", tags=())
        stored = create_sync_plan(
            session, [remote], selector={"mode": "ids", "ids": [201]},
            fields=("rate", "comment"),
        )
        assert stored.plan.format_version == 2
        assert stored.planned[0].action["values"] == {
            "rate": 8,
            "comment": "public reason",
        }

    other_database = _database(tmp_path / "stale")
    stale_queue = _rating_queue(other_database, (202,))
    with session_scope(other_database) as session:
        state = session.get(BangumiCollectionState, 202)
        assert state is not None
        state.rating = 4
    with session_scope(other_database) as session:
        with pytest.raises(RatingQueueError, match="marked stale"):
            rate_rating_item(
                session, stale_queue, 202, score=7, reason=None,
                skip_reason=True, publish_reason=False, public_comment=None,
                replace_existing_comment=False,
            )
        stale_item = session.scalar(
            select(RatingQueueItem).where(RatingQueueItem.session_id == stale_queue)
        )
        assert stale_item is not None and stale_item.item_status == "stale"


def test_rating_existing_comment_requires_explicit_full_replacement(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        pull_collections(
            session, [make_remote_collection(211, rate=0, comment="existing", tags=())]
        )
        seeds = prepare_rating_queue(
            session, subject_types=(4,), collection_statuses=(2,),
            include_deferred=False, order_name="title", random_seed=None,
            max_items=None,
        )
        view = create_rating_queue(
            session, seeds, selector={}, order_name="title", random_seed=None
        )
        rate_rating_item(
            session, view.session.id, 211, score=7, reason="private reason",
            skip_reason=False, publish_reason=True, public_comment=None,
            replace_existing_comment=False,
        )
        state = session.get(BangumiCollectionState, 211)
        assert state is not None and state.comment == "existing"

    other_database = _database(tmp_path / "replace")
    with session_scope(other_database) as session:
        pull_collections(
            session, [make_remote_collection(212, rate=0, comment="existing", tags=())]
        )
        seeds = prepare_rating_queue(
            session, subject_types=(4,), collection_statuses=(2,),
            include_deferred=False, order_name="title", random_seed=None,
            max_items=None,
        )
        view = create_rating_queue(
            session, seeds, selector={}, order_name="title", random_seed=None
        )
        with pytest.raises(RatingQueueError, match="full intended"):
            rate_rating_item(
                session, view.session.id, 212, score=7, reason="private reason",
                skip_reason=False, publish_reason=True, public_comment=None,
                replace_existing_comment=True,
            )


def test_rating_cli_persists_stale_marker(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    queue_id = _rating_queue(database_url, (221,))
    with session_scope(database_url) as session:
        collection = session.get(BangumiCollectionState, 221)
        assert collection is not None
        collection.rating = 3
    result = CliRunner().invoke(
        app,
        ["rating", "queue", "rate", queue_id, "221", "--score", "8"],
        env={"BLD_DATABASE_URL": database_url},
    )
    assert result.exit_code == 1
    with session_scope(database_url) as session:
        item = session.scalar(
            select(RatingQueueItem).where(RatingQueueItem.session_id == queue_id)
        )
        assert item is not None and item.item_status == "stale"

    comment_database = _database(tmp_path / "comment-stale")
    comment_queue = _rating_queue(comment_database, (222,))
    with session_scope(comment_database) as session:
        collection = session.get(BangumiCollectionState, 222)
        assert collection is not None
        collection.comment = "changed elsewhere"
    result = CliRunner().invoke(
        app,
        ["rating", "queue", "skip", comment_queue, "222"],
        env={"BLD_DATABASE_URL": comment_database},
    )
    assert result.exit_code == 1
    with session_scope(comment_database) as session:
        item = session.scalar(
            select(RatingQueueItem).where(RatingQueueItem.session_id == comment_queue)
        )
        assert item is not None and item.item_status == "stale"


def _steam_rows(database_url: str) -> None:
    with session_scope(database_url) as session:
        now = "2026-08-28T00:00:00Z"
        account = SourceAccount(
            source="steam", external_account_id="123", account_name=None,
            config_json="{}", first_seen_at=now, last_seen_at=now,
        )
        session.add(account)
        session.flush()
        collection = LibraryCollection(
            source_account_id=account.id, external_id="done", name="完结-已购",
            kind="manual", active=True, first_seen_at=now, last_seen_at=now,
        )
        session.add(collection)
        session.flush()
        for appid, playtime in (("100", 700), ("200", 0), ("300", 60), ("400", 0)):
            entry = LibraryEntry(
                source_account_id=account.id, external_id=appid,
                title_observed=f"Game {appid}", localized_titles_json="{}",
                ownership_scope="owned", installed=appid in {"100", "400"},
                playtime_minutes=playtime, last_played_at=now if appid == "100" else None,
                metadata_source=None, metadata_json="{}", source_hash=f"hash-{appid}",
                match_status="unmatched", match_reason=None, match_updated_at=None,
                first_seen_at=now, last_seen_at=now,
            )
            session.add(entry)
            session.flush()
            if appid == "100":
                session.add(
                    LibraryEntryCollection(
                        library_entry_id=entry.id, collection_id=collection.id,
                        active=True, first_seen_at=now, last_seen_at=now,
                    )
                )


def test_steam_discovery_priority_decision_suppression_and_reopen(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    _steam_rows(database_url)
    with session_scope(database_url) as session:
        seeds = steam_discovery_seeds(
            session, account_id="123", include_owned_unplayed=False,
            include_decided=False, max_items=50,
        )
        assert len(seeds) == 3
        assert seeds[0].priority_score == 240  # category + >10h + install + owned
        assert [seed.priority_score for seed in seeds] == [240, 80, 60]
        with_owned_only = steam_discovery_seeds(
            session, account_id="123", include_owned_unplayed=True,
            include_decided=False, max_items=50,
        )
        assert [seed.priority_score for seed in with_owned_only] == [240, 80, 60, 20]
        view = create_discovery_session(
            session, provider="steam", filters={}, seeds=seeds[:1]
        )
        candidate = view.candidates[0]
        decide_discovery_candidate(
            session, view.session.id, candidate.id,
            decision="played", reason="remembered",
        )
        assert view.session.status == "completed"
        assert promotion_preview(session, candidate.id).status == "needs_steam_match"
        assert session.scalar(select(DiscoveryReviewEvent)) is not None
        key = candidate.candidate_key

    with session_scope(database_url) as session:
        remaining = steam_discovery_seeds(
            session, account_id="123", include_owned_unplayed=False,
            include_decided=False, max_items=50,
        )
        assert key not in {seed.candidate_key for seed in remaining}
        review = reopen_discovery_candidate(session, key, reason="check again")
        assert review.decision is None
        reopened = steam_discovery_seeds(
            session, account_id="123", include_owned_unplayed=False,
            include_decided=False, max_items=50,
        )
        assert key in {seed.candidate_key for seed in reopened}


@pytest.mark.parametrize("decision", ["played", "not_played", "unsure", "deferred"])
def test_all_discovery_decisions_are_durable(tmp_path: Path, decision: str) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        seed = SubjectSearchCandidate(
            subject_id=501, subject_type=SubjectType.GAME,
            title_original="Candidate", title_cn=None, summary=None,
            release_date=None, cover_url=None,
        )
        seeds = bangumi_discovery_seeds(
            session, [seed], provider="bangumi_search", include_decided=False
        )
        view = create_discovery_session(
            session, provider="bangumi_search", filters={"query": "Candidate"}, seeds=seeds
        )
        decide_discovery_candidate(
            session, view.session.id, view.candidates[0].id,
            decision=decision, reason="private",
        )
    with session_scope(database_url) as session:
        review = session.scalar(select(DiscoveryReviewState))
        assert review is not None and review.decision == decision
        assert not bangumi_discovery_seeds(
            session, [seed], provider="bangumi_search", include_decided=False
        )


def test_discovery_cli_accepts_documented_not_played_spelling(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        candidate = SubjectSearchCandidate(
            subject_id=502, subject_type=SubjectType.GAME,
            title_original="Candidate", title_cn=None, summary=None,
            release_date=None, cover_url=None,
        )
        seeds = bangumi_discovery_seeds(
            session, [candidate], provider="bangumi_search", include_decided=False
        )
        view = create_discovery_session(
            session, provider="bangumi_search", filters={"query": "Candidate"}, seeds=seeds
        )
        session_id = view.session.id
        candidate_id = view.candidates[0].id
    result = CliRunner().invoke(
        app,
        [
            "discovery", "decide", session_id, candidate_id,
            "--decision", "not-played",
        ],
        env={"BLD_DATABASE_URL": database_url},
    )
    assert result.exit_code == 0
    with session_scope(database_url) as session:
        review = session.scalar(select(DiscoveryReviewState))
        assert review is not None and review.decision == "not_played"


def test_discovery_failure_and_identity_convergence_conflict(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        failed = create_failed_discovery_session(
            session, provider="bangumi_search", filters={"query": "broken"},
            error="sanitized API failure",
        )
        assert failed.status == "failed" and failed.item_count == 0
        session.add_all(
            [
                DiscoveryReviewState(
                    id="review-one", candidate_key="legacy:one", work_id=None,
                    subject_id=601, library_entry_id=None, decision="played",
                    reason_private=None, decided_at="2026-08-28T00:00:00Z",
                    updated_at="2026-08-28T00:00:00Z",
                ),
                DiscoveryReviewState(
                    id="review-two", candidate_key="legacy:two", work_id=None,
                    subject_id=601, library_entry_id=None, decision="not_played",
                    reason_private=None, decided_at="2026-08-28T00:00:00Z",
                    updated_at="2026-08-28T00:00:00Z",
                ),
            ]
        )
        session.flush()
        candidate = SubjectSearchCandidate(
            subject_id=601, subject_type=SubjectType.GAME,
            title_original="Converged", title_cn=None, summary=None,
            release_date=None, cover_url=None,
        )
        seeds = bangumi_discovery_seeds(
            session, [candidate], provider="bangumi_search", include_decided=False
        )
        assert len(seeds) == 1 and seeds[0].item_status == "identity_conflict"
        view = create_discovery_session(
            session, provider="bangumi_search", filters={"query": "Converged"}, seeds=seeds
        )
        row = session.get(DiscoveryCandidate, view.candidates[0].id)
        assert row is not None and promotion_preview(session, row.id).status == "identity_conflict"


def test_bangumi_discovery_excludes_collections_and_previews_identity(tmp_path: Path) -> None:
    database_url = _database(tmp_path)
    with session_scope(database_url) as session:
        pull_collections(
            session, [make_remote_collection(301, rate=0, comment="", tags=())]
        )
        candidates = [
            SubjectSearchCandidate(
                subject_id=value, subject_type=SubjectType.GAME,
                title_original=f"Game {value}", title_cn=None, summary=None,
                release_date="2020-01-01", cover_url=None,
            )
            for value in (301, 302)
        ]
        seeds = bangumi_discovery_seeds(
            session, candidates, provider="bangumi_search", include_decided=False
        )
        assert [item.subject_id for item in seeds] == [302]
        view = create_discovery_session(
            session, provider="bangumi_search", filters={"query": "Game"}, seeds=seeds
        )
        preview = promotion_preview(session, view.candidates[0].id)
        assert preview.status == "bangumi_identity_available"
        assert session.scalar(select(DiscoveryReviewState)) is None

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from conftest import make_remote_collection
from bangumi_local.db.models import Base, ChangePlanItem
from bangumi_local.domain.models import RemoteCollection, SubjectType
from bangumi_local.domain.tags import (
    DEFAULT_GALGAME_CLASSIFICATION_TAG,
    DEFAULT_GAME_CLASSIFICATION_TAG,
)
from bangumi_local.services.plans import (
    PlanError,
    create_bulk_tag_plan,
    create_classification_plan,
    export_plan,
    load_plan,
    review_plan,
)
from bangumi_local.services.pull import pull_collections
from bangumi_local.services.plan_revisions import revise_plan_selection


def _with_public_tags(remote: RemoteCollection, *tags: str) -> RemoteCollection:
    return replace(remote, subject=replace(remote.subject, public_tags=tuple(tags)))


def test_bulk_plan_persists_noops_exports_and_detects_tampering(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'plans.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    remotes = [
        make_remote_collection(subject_id=101, tags=("RPG",)),
        make_remote_collection(subject_id=102, tags=("Galgame", "RPG")),
    ]
    with Session(engine) as session:
        pull_collections(session, remotes)
        session.commit()
        stored = create_bulk_tag_plan(
            session,
            remotes,
            operation="add",
            selector={"mode": "all_current"},
            detail_loader=lambda _subject_id: (),
            tag="Galgame",
        )
        session.commit()
        assert len(stored.planned) == 1
        assert stored.planned[0].after_tags == ("RPG", "Galgame")
        assert len(stored.unchanged) == 1
        assert stored.unchanged[0].reason == "no_op"

        json_path, csv_path = export_plan(stored, tmp_path / "exports")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert {item["disposition"] for item in payload["items"]} == {"planned", "unchanged"}
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 2
        assert {row["disposition"] for row in rows} == {"planned", "unchanged"}

        row = session.scalar(
            select(ChangePlanItem).where(ChangePlanItem.plan_id == stored.plan.id)
        )
        assert row is not None
        row.title = "tampered"
        session.commit()
        with pytest.raises(PlanError, match="hash verification"):
            load_plan(session, stored.plan.id)


def test_classification_only_plans_unclassified_public_matches(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'classify.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    remotes = [
        _with_public_tags(make_remote_collection(subject_id=101, tags=()), "Galgame"),
        make_remote_collection(subject_id=102, tags=(DEFAULT_GALGAME_CLASSIFICATION_TAG,)),
        make_remote_collection(subject_id=103, tags=(DEFAULT_GAME_CLASSIFICATION_TAG,)),
        _with_public_tags(make_remote_collection(subject_id=104, tags=()), "RPG"),
        _with_public_tags(make_remote_collection(subject_id=105, tags=()), "RPG"),
    ]
    detail_calls: list[int] = []

    def detail_loader(subject_id: int) -> tuple[str, ...]:
        detail_calls.append(subject_id)
        return ("galGAME",) if subject_id == 104 else ("RPG",)

    with Session(engine) as session:
        pull_collections(session, remotes)
        session.commit()
        stored = create_classification_plan(
            session,
            remotes,
            public_tag="Galgame",
            galgame_tag=DEFAULT_GALGAME_CLASSIFICATION_TAG,
            game_tag=DEFAULT_GAME_CLASSIFICATION_TAG,
            detail_loader=detail_loader,
        )
        session.commit()
        by_id = {item.subject_id: item for item in stored.candidates}
        assert {item.subject_id for item in stored.planned} == {101, 104}
        assert by_id[102].reason == "already_galgame"
        assert by_id[103].reason == "already_game"
        assert by_id[105].reason == "public_tag_not_matched_manual_review"
        assert detail_calls == [104, 105]
        assert len(stored.planned) + len(stored.unchanged) == len(remotes)


def test_classification_workbench_decision_creates_immutable_successor(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'classify-revision.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    remote = _with_public_tags(make_remote_collection(subject_id=106, tags=()), "RPG")
    with Session(engine) as session:
        pull_collections(session, [remote])
        session.commit()
        original = create_classification_plan(
            session,
            [remote],
            public_tag="Galgame",
            galgame_tag=DEFAULT_GALGAME_CLASSIFICATION_TAG,
            game_tag=DEFAULT_GAME_CLASSIFICATION_TAG,
            detail_loader=lambda _subject_id: ("RPG",),
        )
        original_hash = original.plan.content_hash
        successor = revise_plan_selection(
            session,
            original.plan.id,
            included_subject_ids=set(),
            classification_decisions={106: "game"},
        )
        session.commit()
        assert original.plan.status == "cancelled"
        assert original.plan.content_hash == original_hash
        assert successor.planned[0].action["tag"] == DEFAULT_GAME_CLASSIFICATION_TAG
        assert successor.planned[0].reason == "manual_classification"


def test_custom_personal_tag_can_use_independent_custom_public_tag(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'custom-tags.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    remotes = [
        _with_public_tags(make_remote_collection(subject_id=201, tags=()), "Visual Novel"),
        _with_public_tags(make_remote_collection(subject_id=202, tags=()), "RPG"),
    ]
    with Session(engine) as session:
        pull_collections(session, remotes)
        session.commit()
        stored = create_bulk_tag_plan(
            session,
            remotes,
            operation="add",
            selector={"mode": "public_tag", "public_tag": "visual novel"},
            detail_loader=lambda _subject_id: ("RPG",),
            tag="我的VN分类",
        )
        assert [item.subject_id for item in stored.planned] == [201]
        assert stored.planned[0].after_tags == ("我的VN分类",)
        assert [item.subject_id for item in stored.unchanged] == [202]
        assert stored.plan.tag == "我的VN分类"


def test_bulk_tag_subject_type_scope_is_hashed_and_rejects_mismatched_ids(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'typed-tags.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    game = make_remote_collection(subject_id=301, tags=())
    anime = replace(
        make_remote_collection(subject_id=302, tags=()),
        subject_type=SubjectType.ANIME,
    )
    with Session(engine) as session:
        pull_collections(session, [game, anime])
        session.commit()
        scoped = create_bulk_tag_plan(
            session,
            [game, anime],
            operation="add",
            selector={"mode": "all_current", "subject_type": 4},
            detail_loader=lambda _subject_id: (),
            tag="Game only",
        )
        unscoped = create_bulk_tag_plan(
            session,
            [game, anime],
            operation="add",
            selector={"mode": "all_current"},
            detail_loader=lambda _subject_id: (),
            tag="All types",
        )
        assert [item.subject_id for item in scoped.candidates] == [301]
        assert json.loads(scoped.plan.selector_json)["subject_type"] == 4
        assert scoped.plan.content_hash != unscoped.plan.content_hash
        with pytest.raises(PlanError, match="within subject type game"):
            create_bulk_tag_plan(
                session,
                [game, anime],
                operation="add",
                selector={"mode": "ids", "ids": [302], "subject_type": 4},
                detail_loader=lambda _subject_id: (),
                tag="Wrong type",
            )


def test_review_requires_actionable_immutable_plan(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'review.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    remote = make_remote_collection(tags=("Galgame",))
    with Session(engine) as session:
        pull_collections(session, [remote])
        session.commit()
        no_op = create_bulk_tag_plan(
            session,
            [remote],
            operation="add",
            selector={"mode": "all_current"},
            detail_loader=lambda _subject_id: (),
            tag="Galgame",
        )
        session.commit()
        with pytest.raises(PlanError, match="no actionable"):
            review_plan(session, no_op.plan.id)

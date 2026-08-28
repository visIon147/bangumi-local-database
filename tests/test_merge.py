from __future__ import annotations

import pytest

from bangumi_local.domain.merge import DiffStatus, advance_base_after_pull, diff_collection
from bangumi_local.domain.snapshots import CollectionSnapshot


def _snapshot(rate: int, tags: tuple[str, ...] = ("RPG",)) -> CollectionSnapshot:
    return CollectionSnapshot.create(
        collection_type=2,
        rating=rate,
        comment="good",
        is_private=False,
        tags=tags,
    )


@pytest.mark.parametrize(
    ("local_rate", "remote_rate", "expected"),
    (
        (8, 8, DiffStatus.CLEAN),
        (8, 9, DiffStatus.REMOTE_CHANGED),
        (9, 8, DiffStatus.LOCAL_CHANGED),
        (9, 9, DiffStatus.CONVERGED),
        (9, 7, DiffStatus.CONFLICT),
    ),
)
def test_three_way_rating_truth_table(
    local_rate: int,
    remote_rate: int,
    expected: DiffStatus,
) -> None:
    diff = diff_collection(_snapshot(8), _snapshot(local_rate), _snapshot(remote_rate))

    rate = next(field for field in diff.fields if field.field == "rate")
    assert rate.status == expected
    assert diff.status == expected


def test_tag_diff_is_order_insensitive_and_reports_set_changes() -> None:
    base = _snapshot(8, ("RPG", "单机"))
    clean = diff_collection(base, _snapshot(8, ("单机", "RPG")), base)
    changed = diff_collection(
        base,
        _snapshot(8, ("RPG", "本地新增")),
        _snapshot(8, ("RPG", "远端新增")),
    )

    assert clean.status == DiffStatus.CLEAN
    tags = next(field for field in changed.fields if field.field == "tags")
    assert tags.status == DiffStatus.CONFLICT
    assert tags.tag_changes is not None
    assert tags.tag_changes.local_add == ("本地新增",)
    assert tags.tag_changes.local_remove == ("单机",)
    assert tags.tag_changes.remote_add == ("远端新增",)
    assert tags.tag_changes.remote_remove == ("单机",)


def test_advance_base_only_accepts_remote_changed_and_converged_fields() -> None:
    base = _snapshot(8)
    local = CollectionSnapshot.create(
        collection_type=2,
        rating=9,
        comment="remote comment",
        is_private=False,
        tags=("RPG",),
    )
    remote = CollectionSnapshot.create(
        collection_type=2,
        rating=8,
        comment="remote comment",
        is_private=True,
        tags=("RPG",),
    )
    diff = diff_collection(base, local, remote)

    advanced = advance_base_after_pull(diff)

    assert advanced.rating == 8
    assert advanced.comment == "remote comment"
    assert advanced.is_private is True


def test_missing_base_and_remote_have_machine_readable_statuses() -> None:
    snapshot = _snapshot(8)

    bootstrap = diff_collection(None, snapshot, snapshot)
    remote_missing = diff_collection(snapshot, snapshot, None)

    assert bootstrap.status == DiffStatus.BOOTSTRAP_MISSING
    assert bootstrap.to_dict()["status"] == "bootstrap_missing"
    assert remote_missing.status == DiffStatus.REMOTE_MISSING
    assert remote_missing.to_dict()["remote"] is None

from bangumi_local.domain.snapshots import CollectionSnapshot


def test_canonical_snapshot_normalizes_tags_comment_and_unrated() -> None:
    first = CollectionSnapshot.create(
        collection_type=2,
        rating=0,
        comment=None,
        is_private=False,
        tags=[" 单机 ", "RPG", "单机"],
    )
    second = CollectionSnapshot.create(
        collection_type=2,
        rating=None,
        comment="",
        is_private=False,
        tags=["RPG", "单机"],
    )

    assert first == second
    assert first.as_dict()["rate"] == 0
    assert first.digest() == second.digest()


from __future__ import annotations

from datetime import datetime

from bangumi_local.domain.models import CollectionStatus, RemoteCollection, RemoteSubject, SubjectType


def make_remote_collection(
    subject_id: int = 101,
    *,
    rate: int = 8,
    tags: tuple[str, ...] = ("RPG", "单机"),
    status: CollectionStatus = CollectionStatus.DONE,
    comment: str | None = "good",
    private: bool = False,
) -> RemoteCollection:
    return RemoteCollection(
        subject_id=subject_id,
        subject_type=SubjectType.GAME,
        status=status,
        rate=rate,
        comment=comment,
        tags=tags,
        updated_at=datetime.fromisoformat("2025-01-02T03:04:05+08:00"),
        private=private,
        subject=RemoteSubject(
            subject_id=subject_id,
            title_original=f"Game {subject_id}",
            title_cn=f"游戏 {subject_id}",
            summary="summary",
            release_date="2024-01-01",
            cover_url="https://lain.bgm.tv/pic/cover/common/example.jpg",
        ),
    )

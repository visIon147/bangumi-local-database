from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum


class CollectionStatus(IntEnum):
    WISH = 1
    DONE = 2
    DOING = 3
    ON_HOLD = 4
    DROPPED = 5

    @property
    def label(self) -> str:
        return {
            self.WISH: "wish",
            self.DONE: "done",
            self.DOING: "doing",
            self.ON_HOLD: "on-hold",
            self.DROPPED: "dropped",
        }[self]


class SubjectType(IntEnum):
    BOOK = 1
    ANIME = 2
    MUSIC = 3
    GAME = 4
    REAL = 6

    @property
    def kind(self) -> str:
        return {
            self.BOOK: "book",
            self.ANIME: "anime",
            self.MUSIC: "music",
            self.GAME: "game",
            self.REAL: "real",
        }[self]

    @classmethod
    def parse(cls, value: str | int | SubjectType) -> SubjectType:
        if isinstance(value, cls):
            return value
        normalized_value = str(value).strip()
        if isinstance(value, int) or normalized_value.isdigit():
            try:
                return cls(int(normalized_value))
            except ValueError as exc:
                raise ValueError("subject type must be one of 1,2,3,4,6") from exc
        normalized = normalized_value.lower().replace("-", "_")
        aliases = {member.kind: member for member in cls}
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValueError("subject type must be book, anime, music, game, real or 1,2,3,4,6") from exc


@dataclass(frozen=True, slots=True)
class RemoteUser:
    id: int
    username: str
    nickname: str


@dataclass(frozen=True, slots=True)
class RemoteSubject:
    subject_id: int
    title_original: str
    title_cn: str | None
    summary: str | None
    release_date: str | None
    cover_url: str | None
    metadata_available: bool = True
    public_tags: tuple[str, ...] = ()

    @property
    def display_title(self) -> str:
        return self.title_cn or self.title_original


@dataclass(frozen=True, slots=True)
class SubjectSearchCandidate:
    subject_id: int
    subject_type: SubjectType
    title_original: str
    title_cn: str | None
    summary: str | None
    release_date: str | None
    cover_url: str | None
    aliases: tuple[str, ...] = ()
    public_tags: tuple[str, ...] = ()
    rank: int | None = None
    score: float | None = None
    rating_count: int | None = None

    @property
    def display_title(self) -> str:
        return self.title_cn or self.title_original

    @property
    def url(self) -> str:
        return f"https://bgm.tv/subject/{self.subject_id}"


@dataclass(frozen=True, slots=True)
class RemoteCollection:
    subject_id: int
    subject_type: SubjectType
    status: CollectionStatus
    rate: int
    comment: str | None
    tags: tuple[str, ...]
    updated_at: datetime
    private: bool
    subject: RemoteSubject

    @property
    def updated_at_utc(self) -> str:
        value = self.updated_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @property
    def game(self) -> RemoteSubject:
        """Phase 1–3 read-only compatibility alias."""
        return self.subject


# Transitional alias for integrations that still import the Phase 1 name.
RemoteGame = RemoteSubject

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ImagePolicy(StrEnum):
    NONE = "none"
    METADATA = "metadata"
    CACHE = "cache"

    @classmethod
    def parse(cls, value: str | ImagePolicy) -> ImagePolicy:
        if isinstance(value, cls):
            return value
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            raise ValueError("image policy must be none, metadata, or cache") from exc


@dataclass(frozen=True, slots=True)
class MediaReference:
    provider: str
    external_id: str
    variant: str
    origin: str
    locale: str = ""
    remote_url: str | None = None
    logical_locator: dict[str, object] | None = None

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (self.provider, self.external_id, self.variant, self.locale, self.origin)


@dataclass(frozen=True, slots=True)
class LocalMediaCandidate:
    reference: MediaReference
    source_path: Path


@dataclass(frozen=True, slots=True)
class CachedMedia:
    sha256: str
    storage_relpath: str
    mime_type: str
    byte_size: int
    width: int | None
    height: int | None


@dataclass(frozen=True, slots=True)
class MediaRegistrationSummary:
    examined: int
    sources_created: int
    sources_updated: int
    cached: int
    skipped: int


@dataclass(frozen=True, slots=True)
class MediaStatusSummary:
    source_count: int
    cached_source_count: int
    failed_source_count: int
    missing_source_count: int
    blob_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class MediaVerifyIssue:
    sha256: str
    code: str


@dataclass(frozen=True, slots=True)
class MediaPruneResult:
    apply: bool
    blob_count: int
    byte_count: int
    sha256s: tuple[str, ...]

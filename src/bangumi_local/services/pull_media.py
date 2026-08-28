from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bangumi_local.db.models import BangumiSubject, MediaSource
from bangumi_local.db.session import session_scope
from bangumi_local.domain.media import MediaReference
from bangumi_local.domain.models import RemoteCollection
from bangumi_local.services.media import (
    MediaError,
    bind_media_source,
    download_remote_media,
    normalize_remote_image_url,
    register_media_references,
)


@dataclass(frozen=True, slots=True)
class PullMediaResult:
    registered: int
    downloaded: int
    reused: int
    failed: int


def materialize_pull_media(
    database_url: str,
    collections: list[RemoteCollection],
    *,
    policy: str,
    cache_directory: Path,
    max_bytes: int,
    timeout_seconds: float,
    progress: Callable[[int, int], None] | None = None,
) -> PullMediaResult:
    """Register public cover metadata and optionally download without duplicate GETs."""

    if policy == "none":
        return PullMediaResult(0, 0, 0, 0)
    if policy not in {"metadata", "missing", "refresh"}:
        raise ValueError("Unsupported pull image policy.")
    references: list[MediaReference] = []
    for item in collections:
        if not item.subject.cover_url:
            continue
        try:
            url = normalize_remote_image_url(item.subject.cover_url, "bangumi")
        except MediaError:
            continue
        references.append(
            MediaReference(
                provider="bangumi",
                external_id=str(item.subject_id),
                variant="preferred",
                origin="remote",
                remote_url=url,
            )
        )
    with session_scope(database_url) as session:
        register_media_references(session, references, policy="metadata")

    downloaded = reused = failed = 0
    if policy in {"missing", "refresh"}:
        for index, reference in enumerate(references, 1):
            if progress is not None:
                progress(index - 1, len(references))
            with session_scope(database_url) as session:
                source = session.query(MediaSource).filter_by(
                    provider=reference.provider,
                    external_id=reference.external_id,
                    variant=reference.variant,
                    locale=reference.locale,
                    origin=reference.origin,
                ).one_or_none()
                cached = source is not None and source.current_blob_sha256 is not None
            if policy == "missing" and cached:
                reused += 1
                continue
            try:
                materialized = download_remote_media(
                    reference,
                    cache_directory,
                    max_bytes=max_bytes,
                    timeout_seconds=timeout_seconds,
                )
            except MediaError:
                failed += 1
                continue
            with session_scope(database_url) as session:
                register_media_references(
                    session,
                    (reference,),
                    policy="cache",
                    cached={reference.key: materialized},
                )
            downloaded += 1

    with session_scope(database_url) as session:
        for reference in references:
            source = session.query(MediaSource).filter_by(
                provider=reference.provider,
                external_id=reference.external_id,
                variant=reference.variant,
                locale=reference.locale,
                origin=reference.origin,
            ).one_or_none()
            identity = session.get(BangumiSubject, int(reference.external_id))
            if source is not None and identity is not None:
                bind_media_source(
                    session,
                    source.id,
                    work_id=identity.work_id,
                    role="cover",
                    priority=100,
                )
    if progress is not None:
        progress(len(references), len(references))
    return PullMediaResult(len(references), downloaded, reused, failed)

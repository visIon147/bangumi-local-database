from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import time

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from bangumi_local.adapters.steam import (
    SteamDataError,
    SteamStoreMedia,
    fetch_store_media,
)
from bangumi_local.db.models import (
    BangumiSubject,
    LibraryEntry,
    MediaBinding,
    MediaBlob,
    MediaSource,
)
from bangumi_local.domain.media import CachedMedia, MediaReference
from bangumi_local.services.media import (
    MediaError,
    bind_media_source,
    download_remote_media,
    mark_media_source_failure,
    normalize_remote_image_url,
    register_media_references,
)
from bangumi_local.services.steam_library import steam_account


class SteamCoverError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SteamCoverTarget:
    entry_id: int
    app_id: str


@dataclass(frozen=True, slots=True)
class SteamCoverPreparation:
    targets: tuple[SteamCoverTarget, ...]
    examined: int
    already_available: int


@dataclass(frozen=True, slots=True)
class SteamCoverFailure:
    reference: MediaReference
    code: str


@dataclass(frozen=True, slots=True)
class SteamCoverOutcome:
    target: SteamCoverTarget
    status: str
    reference: MediaReference | None = None
    cached: CachedMedia | None = None
    failures: tuple[SteamCoverFailure, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SteamCoverSummary:
    examined: int
    requested: int
    cached: int
    already_available: int
    no_metadata: int
    failed: int
    still_missing: tuple[str, ...]


def _blob_available(cache_directory: Path, blob: MediaBlob | None) -> bool:
    if blob is None:
        return False
    root = cache_directory.expanduser().resolve()
    target = (root / blob.storage_relpath).resolve()
    return root in target.parents and target.is_file()


def _effective_cover_available(
    session: Session, cache_directory: Path, entry: LibraryEntry
) -> bool:
    binding_condition = MediaBinding.library_entry_id == entry.id
    if entry.work_id is not None:
        binding_condition = or_(binding_condition, MediaBinding.work_id == entry.work_id)
    for binding, source in session.execute(
        select(MediaBinding, MediaSource)
        .join(MediaSource, MediaSource.id == MediaBinding.media_source_id)
        .where(binding_condition)
    ):
        digest = binding.pinned_blob_sha256 or source.current_blob_sha256
        if digest and _blob_available(cache_directory, session.get(MediaBlob, digest)):
            return True

    external_keys = [("steam", entry.external_id)]
    if entry.work_id is not None:
        identity = session.scalar(
            select(BangumiSubject).where(BangumiSubject.work_id == entry.work_id)
        )
        if identity is not None:
            external_keys.append(("bangumi", str(identity.subject_id)))
    for provider, external_id in external_keys:
        for source in session.scalars(
            select(MediaSource).where(
                MediaSource.provider == provider,
                MediaSource.external_id == external_id,
                MediaSource.current_blob_sha256.is_not(None),
            )
        ):
            if _blob_available(
                cache_directory, session.get(MediaBlob, source.current_blob_sha256)
            ):
                return True
    return False


def prepare_steam_cover_completion(
    session: Session,
    cache_directory: Path,
    *,
    account_id: str | None,
    app_ids: tuple[str, ...] | None,
    all_missing: bool,
    force_refresh: bool,
    max_items: int,
) -> SteamCoverPreparation:
    if (app_ids is None) == (not all_missing):
        raise SteamCoverError("Choose exactly one of AppIDs or all missing covers.")
    if not 1 <= max_items <= 250:
        raise SteamCoverError("Steam cover max_items must be between 1 and 250.")
    selected_ids = tuple(dict.fromkeys(app_ids or ()))
    if any(not app_id.isdigit() for app_id in selected_ids):
        raise SteamCoverError("Steam AppIDs must be numeric.")
    account = steam_account(session, account_id)
    entries = list(
        session.scalars(
            select(LibraryEntry)
            .where(LibraryEntry.source_account_id == account.id)
            .order_by(LibraryEntry.external_id)
        )
    )
    if selected_ids:
        by_app_id = {entry.external_id: entry for entry in entries}
        missing_ids = [app_id for app_id in selected_ids if app_id not in by_app_id]
        if missing_ids:
            raise SteamCoverError(
                "Steam AppIDs are not present in the selected local account: "
                + ", ".join(missing_ids)
            )
        entries = [by_app_id[app_id] for app_id in selected_ids]

    targets: list[SteamCoverTarget] = []
    already_available = 0
    for entry in entries:
        available = _effective_cover_available(session, cache_directory, entry)
        if available and (all_missing or not force_refresh):
            already_available += 1
            continue
        targets.append(SteamCoverTarget(entry.id, entry.external_id))
    if len(targets) > max_items:
        raise SteamCoverError(
            f"Steam cover selection contains {len(targets)} items; narrow it to {max_items} or fewer."
        )
    return SteamCoverPreparation(tuple(targets), len(entries), already_available)


def steam_store_cover_references(media: SteamStoreMedia) -> tuple[MediaReference, ...]:
    base = (
        "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/"
        f"{media.app_id}"
    )
    candidates = (
        ("library_portrait_2x", f"{base}/library_600x900_2x.jpg"),
        ("library_portrait", f"{base}/library_600x900.jpg"),
        ("header", media.header_image),
        ("capsule", media.capsule_image),
    )
    references: list[MediaReference] = []
    seen: set[str] = set()
    for variant, raw_url in candidates:
        if not raw_url:
            continue
        try:
            url = normalize_remote_image_url(raw_url, "steam")
        except MediaError:
            continue
        if url in seen:
            continue
        seen.add(url)
        references.append(
            MediaReference(
                provider="steam",
                external_id=media.app_id,
                variant=variant,
                origin="remote",
                remote_url=url,
            )
        )
    return tuple(references)


def _retryable(code: str) -> bool:
    if code in {
        "steam_store_timeout",
        "steam_store_transport_error",
        "media_download_failed",
    }:
        return True
    for prefix in ("steam_store_http_", "media_http_"):
        if code.startswith(prefix):
            try:
                status = int(code.removeprefix(prefix))
            except ValueError:
                return False
            return status == 429 or status >= 500
    return False


def _with_retry(
    operation: Callable[[], object],
    *,
    max_retries: int,
    retry_base_seconds: float,
    sleep_fn: Callable[[float], None],
) -> object:
    for attempt in range(max_retries + 1):
        try:
            return operation()
        except (SteamDataError, MediaError) as exc:
            code = str(exc)
            if attempt >= max_retries or not _retryable(code):
                raise
            sleep_fn(retry_base_seconds * (2**attempt))
    raise AssertionError("retry loop exhausted")


def fetch_steam_cover(
    target: SteamCoverTarget,
    cache_directory: Path,
    *,
    max_bytes: int,
    timeout_seconds: float,
    max_retries: int,
    retry_base_seconds: float,
    sleep_fn: Callable[[float], None] = time.sleep,
    store_fetcher: Callable[..., SteamStoreMedia | None] = fetch_store_media,
    downloader: Callable[..., CachedMedia] = download_remote_media,
) -> SteamCoverOutcome:
    try:
        media = _with_retry(
            lambda: store_fetcher(target.app_id, timeout_seconds=timeout_seconds),
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
            sleep_fn=sleep_fn,
        )
    except SteamDataError as exc:
        return SteamCoverOutcome(target, "failed", error=str(exc))
    if media is None:
        return SteamCoverOutcome(target, "no_metadata", error="steam_store_no_metadata")

    failures: list[SteamCoverFailure] = []
    for reference in steam_store_cover_references(media):
        try:
            cached = _with_retry(
                lambda reference=reference: downloader(
                    reference,
                    cache_directory,
                    max_bytes=max_bytes,
                    timeout_seconds=timeout_seconds,
                ),
                max_retries=max_retries,
                retry_base_seconds=retry_base_seconds,
                sleep_fn=sleep_fn,
            )
        except MediaError as exc:
            failures.append(SteamCoverFailure(reference, str(exc)))
            continue
        return SteamCoverOutcome(
            target,
            "cached",
            reference=reference,
            cached=cached,
            failures=tuple(failures),
        )
    return SteamCoverOutcome(
        target,
        "failed",
        failures=tuple(failures),
        error=failures[-1].code if failures else "steam_store_no_images",
    )


def persist_steam_cover(session: Session, outcome: SteamCoverOutcome) -> None:
    for failure in outcome.failures:
        code = failure.code if failure.code.startswith("media_") else "media_download_failed"
        mark_media_source_failure(
            session,
            failure.reference,
            failure_code=code[:32],
            missing=code == "media_remote_missing",
        )
    if outcome.reference is None or outcome.cached is None:
        return
    register_media_references(
        session,
        (outcome.reference,),
        policy="cache",
        cached={outcome.reference.key: outcome.cached},
    )
    source = session.scalar(
        select(MediaSource).where(
            MediaSource.provider == "steam",
            MediaSource.external_id == outcome.target.app_id,
            MediaSource.variant == outcome.reference.variant,
            MediaSource.locale == outcome.reference.locale,
            MediaSource.origin == "remote",
        )
    )
    if source is None:
        raise SteamCoverError("Cached Steam media source was not persisted.")
    bind_media_source(
        session,
        source.id,
        library_entry_id=outcome.target.entry_id,
        role="cover",
        priority=(90 if outcome.reference.variant.startswith("library_portrait") else 60),
    )


def steam_cover_summary(
    preparation: SteamCoverPreparation, outcomes: tuple[SteamCoverOutcome, ...]
) -> SteamCoverSummary:
    return SteamCoverSummary(
        examined=preparation.examined,
        requested=len(preparation.targets),
        cached=sum(item.status == "cached" for item in outcomes),
        already_available=preparation.already_available,
        no_metadata=sum(item.status == "no_metadata" for item in outcomes),
        failed=sum(item.status == "failed" for item in outcomes),
        still_missing=tuple(
            item.target.app_id for item in outcomes if item.status != "cached"
        ),
    )

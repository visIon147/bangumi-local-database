from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import struct
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from bangumi_local.db.models import (
    MediaBinding,
    MediaBlob,
    MediaRendition,
    MediaSource,
)
from bangumi_local.db.repositories import utc_now_iso
from bangumi_local.domain.media import (
    CachedMedia,
    ImagePolicy,
    LocalMediaCandidate,
    MediaPruneResult,
    MediaReference,
    MediaRegistrationSummary,
    MediaStatusSummary,
    MediaVerifyIssue,
)
from bangumi_local.domain.plans import stable_json


class MediaError(RuntimeError):
    """A sanitized media error which does not expose local paths or credentials."""


_ALLOWED_HOSTS = {
    "lain.bgm.tv",
    "cdn.akamai.steamstatic.com",
    "shared.akamai.steamstatic.com",
    "shared.cloudflare.steamstatic.com",
    "store.cloudflare.steamstatic.com",
    "steamcdn-a.akamaihd.net",
}
_MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
_MAX_IMAGE_DIMENSION = 16_384
_MAX_IMAGE_PIXELS = 64_000_000


def normalize_remote_image_url(url: str, provider: str) -> str:
    parsed = urlsplit(url.strip())
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise MediaError("media_url_rejected")
    if parsed.query:
        raise MediaError("media_url_query_rejected")
    if provider not in {"bangumi", "steam"} or hostname not in _ALLOWED_HOSTS:
        raise MediaError("media_host_rejected")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path, "", ""))


def bangumi_image_references(
    subject_id: int, images: Mapping[str, str | None]
) -> tuple[MediaReference, ...]:
    references: list[MediaReference] = []
    for variant in ("large", "common", "medium", "small", "grid"):
        url = images.get(variant)
        if url:
            references.append(
                MediaReference(
                    provider="bangumi",
                    external_id=str(subject_id),
                    variant=variant,
                    origin="remote",
                    remote_url=normalize_remote_image_url(url, "bangumi"),
                )
            )
    return tuple(references)


def steam_remote_image_references(
    app_id: str, images: Mapping[str, str | None]
) -> tuple[MediaReference, ...]:
    if not app_id.isdigit():
        raise MediaError("steam_appid_invalid")
    references: list[MediaReference] = []
    for variant, url in sorted(images.items()):
        if url:
            references.append(
                MediaReference(
                    provider="steam",
                    external_id=app_id,
                    variant=variant,
                    origin="remote",
                    remote_url=normalize_remote_image_url(url, "steam"),
                )
            )
    return tuple(references)


def _local_reference(app_id: str, variant: str, filename: str, locale: str = "") -> MediaReference:
    return MediaReference(
        provider="steam",
        external_id=app_id,
        variant=variant,
        locale=locale,
        origin="steam_local",
        logical_locator={
            "kind": "steam_librarycache",
            "app_id": app_id,
            "filename": filename,
        },
    )


def _portable_locator(reference: MediaReference) -> dict[str, object]:
    locator = reference.logical_locator or {}
    if reference.origin != "steam_local":
        return locator
    expected_keys = {"kind", "app_id", "filename"}
    filename = locator.get("filename")
    if (
        set(locator) != expected_keys
        or locator.get("kind") != "steam_librarycache"
        or locator.get("app_id") != reference.external_id
        or not isinstance(filename, str)
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
    ):
        raise MediaError("media_local_locator_invalid")
    return locator


def scan_steam_librarycache(
    steam_root: Path, *, app_ids: Iterable[str] | None = None
) -> tuple[LocalMediaCandidate, ...]:
    cache_root = steam_root.expanduser().resolve() / "appcache" / "librarycache"
    if not cache_root.is_dir():
        raise MediaError("steam_librarycache_unavailable")
    selected = {str(value) for value in app_ids} if app_ids is not None else None
    if selected is not None and any(not value.isdigit() for value in selected):
        raise MediaError("steam_appid_invalid")
    candidates: list[LocalMediaCandidate] = []
    for app_dir in sorted(cache_root.iterdir(), key=lambda item: item.name):
        app_id = app_dir.name
        if not app_dir.is_dir() or not app_id.isdigit() or (
            selected is not None and app_id not in selected
        ):
            continue
        direct: list[tuple[str, str, str]] = [
            ("library_portrait", "library_600x900.jpg", ""),
            ("header", "header.jpg", ""),
            ("hero", "library_hero.jpg", ""),
            ("logo", "logo.png", ""),
        ]
        for path in sorted(app_dir.glob("header_*.jpg")):
            locale = path.stem.removeprefix("header_")
            direct.append(("header", path.name, locale))
        for variant, filename, locale in direct:
            path = app_dir / filename
            if path.is_file() and cache_root in path.resolve().parents:
                candidates.append(
                    LocalMediaCandidate(
                        _local_reference(app_id, variant, filename, locale), path
                    )
                )
        for variant, filename in (
            ("library_capsule", "library_capsule.jpg"),
            ("library_header", "library_header.jpg"),
        ):
            matches = [
                item
                for item in app_dir.glob(f"*/{filename}")
                if item.is_file() and cache_root in item.resolve().parents
            ]
            if matches:
                # Hash-named asset generations can coexist. Prefer the largest, then a stable name.
                path = sorted(matches, key=lambda item: (item.stat().st_size, str(item)), reverse=True)[0]
                candidates.append(
                    LocalMediaCandidate(_local_reference(app_id, variant, filename), path)
                )
    return tuple(candidates)


def _image_info(payload: bytes) -> tuple[str, int | None, int | None]:
    if payload.startswith(b"\x89PNG\r\n\x1a\n") and len(payload) >= 24:
        width, height = struct.unpack(">II", payload[16:24])
        return "image/png", width, height
    if payload.startswith((b"GIF87a", b"GIF89a")) and len(payload) >= 10:
        width, height = struct.unpack("<HH", payload[6:10])
        return "image/gif", width, height
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp", None, None
    if payload.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(payload):
            if payload[index] != 0xFF:
                index += 1
                continue
            marker = payload[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(payload):
                break
            segment_length = int.from_bytes(payload[index:index + 2], "big")
            if marker in range(0xC0, 0xC4) and index + 7 <= len(payload):
                return (
                    "image/jpeg",
                    int.from_bytes(payload[index + 5:index + 7], "big"),
                    int.from_bytes(payload[index + 3:index + 5], "big"),
                )
            if segment_length < 2:
                break
            index += segment_length
        return "image/jpeg", None, None
    raise MediaError("media_content_invalid")


def _safe_cache_root(cache_directory: Path) -> Path:
    root = cache_directory.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root.parent == root:
        raise MediaError("media_cache_root_unsafe")
    return root


def cache_media_bytes(
    payload: bytes, cache_directory: Path, *, max_bytes: int
) -> CachedMedia:
    if len(payload) > max_bytes:
        raise MediaError("media_too_large")
    mime_type, width, height = _image_info(payload)
    if width is not None and height is not None and (
        width < 1
        or height < 1
        or width > _MAX_IMAGE_DIMENSION
        or height > _MAX_IMAGE_DIMENSION
        or width * height > _MAX_IMAGE_PIXELS
    ):
        raise MediaError("media_dimensions_invalid")
    digest = hashlib.sha256(payload).hexdigest()
    relative = f"{digest[:2]}/{digest}.{_MIME_EXTENSIONS[mime_type]}"
    root = _safe_cache_root(cache_directory)
    target = (root / relative).resolve()
    if root not in target.parents:
        raise MediaError("media_cache_path_unsafe")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return CachedMedia(digest, relative, mime_type, len(payload), width, height)


def cache_local_media(
    candidate: LocalMediaCandidate, cache_directory: Path, *, max_bytes: int
) -> CachedMedia:
    try:
        with candidate.source_path.open("rb") as stream:
            payload = stream.read(max_bytes + 1)
    except OSError as exc:
        raise MediaError("media_local_read_failed") from exc
    return cache_media_bytes(payload, cache_directory, max_bytes=max_bytes)


def download_remote_media(
    reference: MediaReference,
    cache_directory: Path,
    *,
    max_bytes: int,
    timeout_seconds: float = 20.0,
    client: httpx.Client | None = None,
) -> CachedMedia:
    if reference.origin != "remote" or reference.remote_url is None:
        raise MediaError("media_remote_url_missing")
    current_url = normalize_remote_image_url(reference.remote_url, reference.provider)
    owned_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)
    try:
        for _ in range(4):
            try:
                request = http_client.build_request("GET", current_url)
                response = http_client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise MediaError("media_download_failed") from exc
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise MediaError("media_redirect_invalid")
                    current_url = normalize_remote_image_url(
                        str(response.url.join(location)), reference.provider
                    )
                    continue
                if response.status_code == 404:
                    raise MediaError("media_remote_missing")
                if not response.is_success:
                    raise MediaError(f"media_http_{response.status_code}")
                content_length = response.headers.get("content-length")
                try:
                    if content_length and int(content_length) > max_bytes:
                        raise MediaError("media_too_large")
                except ValueError as exc:
                    raise MediaError("media_content_length_invalid") from exc
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > max_bytes:
                        raise MediaError("media_too_large")
                declared_mime = response.headers.get("content-type", "").split(";", 1)[0]
                detected_mime, _, _ = _image_info(bytes(payload))
                if declared_mime and declared_mime != detected_mime:
                    raise MediaError("media_content_type_invalid")
                materialized = cache_media_bytes(
                    bytes(payload), cache_directory, max_bytes=max_bytes
                )
                return materialized
            finally:
                response.close()
        raise MediaError("media_redirect_limit")
    finally:
        if owned_client:
            http_client.close()


def register_media_references(
    session: Session,
    references: Iterable[MediaReference],
    *,
    policy: str | ImagePolicy,
    cached: Mapping[tuple[str, str, str, str, str], CachedMedia] | None = None,
) -> MediaRegistrationSummary:
    normalized_policy = ImagePolicy.parse(policy)
    items = tuple(references)
    if normalized_policy is ImagePolicy.NONE:
        return MediaRegistrationSummary(len(items), 0, 0, 0, len(items))
    cached = cached or {}
    now = utc_now_iso()
    created = updated = cached_count = 0
    for reference in items:
        if reference.provider not in {"bangumi", "steam"}:
            raise MediaError("media_provider_invalid")
        if reference.origin not in {"remote", "steam_local"}:
            raise MediaError("media_origin_invalid")
        remote_url = (
            normalize_remote_image_url(reference.remote_url, reference.provider)
            if reference.remote_url
            else None
        )
        source = session.scalar(
            select(MediaSource).where(
                MediaSource.provider == reference.provider,
                MediaSource.external_id == reference.external_id,
                MediaSource.variant == reference.variant,
                MediaSource.locale == reference.locale,
                MediaSource.origin == reference.origin,
            )
        )
        materialized = cached.get(reference.key) if normalized_policy is ImagePolicy.CACHE else None
        if materialized is not None:
            blob = session.get(MediaBlob, materialized.sha256)
            if blob is None:
                blob = MediaBlob(
                    sha256=materialized.sha256,
                    storage_relpath=materialized.storage_relpath,
                    mime_type=materialized.mime_type,
                    byte_size=materialized.byte_size,
                    width=materialized.width,
                    height=materialized.height,
                    created_at=now,
                    last_verified_at=now,
                    last_accessed_at=now,
                )
                session.add(blob)
                session.flush()
        if source is None:
            source = MediaSource(
                id=str(uuid4()),
                provider=reference.provider,
                external_id=reference.external_id,
                variant=reference.variant,
                locale=reference.locale,
                origin=reference.origin,
                remote_url=remote_url,
                logical_locator_json=stable_json(_portable_locator(reference)),
                status="cached" if materialized else "observed",
                current_blob_sha256=materialized.sha256 if materialized else None,
                observed_at=now,
                last_checked_at=now if materialized else None,
                fetched_at=now if materialized else None,
                failure_count=0,
            )
            session.add(source)
            session.flush()
            created += 1
        else:
            url_changed = source.remote_url != remote_url
            source.remote_url = remote_url
            source.logical_locator_json = stable_json(_portable_locator(reference))
            source.observed_at = now
            if materialized:
                source.status = "cached"
                source.current_blob_sha256 = materialized.sha256
                source.last_checked_at = now
                source.fetched_at = now
                source.failure_code = None
                source.failure_count = 0
            elif url_changed:
                source.status = "stale" if source.current_blob_sha256 else "observed"
            updated += 1
        if materialized:
            rendition = session.get(MediaRendition, (source.id, "original"))
            if rendition is None:
                session.add(
                    MediaRendition(
                        media_source_id=source.id,
                        purpose="original",
                        blob_sha256=materialized.sha256,
                        created_at=now,
                    )
                )
            else:
                rendition.blob_sha256 = materialized.sha256
                rendition.created_at = now
            cached_count += 1
    session.flush()
    return MediaRegistrationSummary(len(items), created, updated, cached_count, 0)


def mark_media_source_failure(
    session: Session,
    reference: MediaReference,
    *,
    failure_code: str,
    missing: bool = False,
    retry_after: str | None = None,
) -> MediaSource:
    if not failure_code.startswith("media_") or len(failure_code) > 32:
        raise MediaError("media_failure_code_invalid")
    register_media_references(session, (reference,), policy="metadata")
    source = session.scalar(
        select(MediaSource).where(
            MediaSource.provider == reference.provider,
            MediaSource.external_id == reference.external_id,
            MediaSource.variant == reference.variant,
            MediaSource.locale == reference.locale,
            MediaSource.origin == reference.origin,
        )
    )
    assert source is not None
    source.status = "missing" if missing else ("stale" if source.current_blob_sha256 else "failed")
    source.failure_code = failure_code
    source.failure_count += 1
    source.retry_after = retry_after
    source.last_checked_at = utc_now_iso()
    session.flush()
    return source


def bind_media_source(
    session: Session,
    source_id: str,
    *,
    role: str,
    work_id: int | None = None,
    library_entry_id: int | None = None,
    rating_queue_item_id: str | None = None,
    discovery_candidate_id: str | None = None,
    priority: int = 0,
    pin_current: bool = False,
) -> MediaBinding:
    targets = (work_id, library_entry_id, rating_queue_item_id, discovery_candidate_id)
    if sum(item is not None for item in targets) != 1:
        raise MediaError("media_binding_target_invalid")
    source = session.get(MediaSource, source_id)
    if source is None:
        raise MediaError("media_source_not_found")
    now = utc_now_iso()
    binding = session.scalar(
        select(MediaBinding).where(
            MediaBinding.media_source_id == source_id,
            MediaBinding.work_id == work_id,
            MediaBinding.library_entry_id == library_entry_id,
            MediaBinding.rating_queue_item_id == rating_queue_item_id,
            MediaBinding.discovery_candidate_id == discovery_candidate_id,
            MediaBinding.role == role,
        )
    )
    if binding is not None:
        binding.priority = priority
        binding.last_observed_at = now
        if pin_current:
            binding.pinned_blob_sha256 = source.current_blob_sha256
        session.flush()
        return binding
    binding = MediaBinding(
        id=str(uuid4()),
        media_source_id=source_id,
        work_id=work_id,
        library_entry_id=library_entry_id,
        rating_queue_item_id=rating_queue_item_id,
        discovery_candidate_id=discovery_candidate_id,
        role=role,
        priority=priority,
        pinned_blob_sha256=source.current_blob_sha256 if pin_current else None,
        first_observed_at=now,
        last_observed_at=now,
    )
    session.add(binding)
    session.flush()
    return binding


def media_status(session: Session) -> MediaStatusSummary:
    source_count = session.scalar(select(func.count()).select_from(MediaSource)) or 0
    blob_count = session.scalar(select(func.count()).select_from(MediaBlob)) or 0
    total_bytes = session.scalar(select(func.coalesce(func.sum(MediaBlob.byte_size), 0))) or 0
    return MediaStatusSummary(
        source_count=source_count,
        cached_source_count=session.scalar(
            select(func.count()).select_from(MediaSource).where(MediaSource.status == "cached")
        ) or 0,
        failed_source_count=session.scalar(
            select(func.count()).select_from(MediaSource).where(MediaSource.status == "failed")
        ) or 0,
        missing_source_count=session.scalar(
            select(func.count()).select_from(MediaSource).where(MediaSource.status == "missing")
        ) or 0,
        blob_count=blob_count,
        total_bytes=total_bytes,
    )


def _blob_path(cache_directory: Path, blob: MediaBlob) -> Path:
    root = cache_directory.expanduser().resolve()
    target = (root / blob.storage_relpath).resolve()
    if root not in target.parents:
        raise MediaError("media_cache_path_unsafe")
    return target


def verify_media_cache(
    session: Session, cache_directory: Path
) -> tuple[MediaVerifyIssue, ...]:
    issues: list[MediaVerifyIssue] = []
    for blob in session.scalars(select(MediaBlob).order_by(MediaBlob.sha256)):
        try:
            target = _blob_path(cache_directory, blob)
        except MediaError:
            issues.append(MediaVerifyIssue(blob.sha256, "unsafe_path"))
            continue
        if not target.is_file():
            issues.append(MediaVerifyIssue(blob.sha256, "missing_file"))
            continue
        try:
            payload = target.read_bytes()
        except OSError:
            issues.append(MediaVerifyIssue(blob.sha256, "read_failed"))
            continue
        if len(payload) != blob.byte_size:
            issues.append(MediaVerifyIssue(blob.sha256, "size_mismatch"))
        elif hashlib.sha256(payload).hexdigest() != blob.sha256:
            issues.append(MediaVerifyIssue(blob.sha256, "digest_mismatch"))
        else:
            try:
                mime, _, _ = _image_info(payload)
            except MediaError:
                issues.append(MediaVerifyIssue(blob.sha256, "invalid_image"))
            else:
                if mime != blob.mime_type:
                    issues.append(MediaVerifyIssue(blob.sha256, "mime_mismatch"))
    return tuple(issues)


def prune_media_cache(
    session: Session,
    cache_directory: Path,
    *,
    max_bytes: int | None = None,
    older_than_days: int | None = None,
    apply: bool = False,
) -> MediaPruneResult:
    if max_bytes is not None and max_bytes < 0:
        raise MediaError("media_prune_limit_invalid")
    if older_than_days is not None and older_than_days < 0:
        raise MediaError("media_prune_age_invalid")
    pinned = set(
        session.scalars(
            select(MediaBinding.pinned_blob_sha256).where(
                MediaBinding.pinned_blob_sha256.is_not(None)
            )
        )
    )
    current = set(
        session.scalars(
            select(MediaSource.current_blob_sha256).where(
                MediaSource.current_blob_sha256.is_not(None)
            )
        )
    )
    rendition = set(session.scalars(select(MediaRendition.blob_sha256)))
    blobs = list(session.scalars(select(MediaBlob).order_by(MediaBlob.last_accessed_at, MediaBlob.sha256)))
    total = sum(blob.byte_size for blob in blobs)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=older_than_days)
        if older_than_days is not None
        else None
    )
    selected: list[MediaBlob] = []
    for blob in blobs:
        if blob.sha256 in pinned:
            continue
        orphan = blob.sha256 not in current and blob.sha256 not in rendition
        old_enough = True
        if cutoff is not None and blob.last_accessed_at:
            try:
                old_enough = datetime.fromisoformat(
                    blob.last_accessed_at.replace("Z", "+00:00")
                ) <= cutoff
            except ValueError:
                old_enough = False
        over_limit = max_bytes is not None and total > max_bytes
        if old_enough and (orphan or over_limit):
            selected.append(blob)
            total -= blob.byte_size
    if apply:
        for blob in selected:
            session.execute(
                delete(MediaRendition).where(MediaRendition.blob_sha256 == blob.sha256)
            )
            for source in session.scalars(
                select(MediaSource).where(MediaSource.current_blob_sha256 == blob.sha256)
            ):
                source.current_blob_sha256 = None
                source.status = "observed"
            session.flush()
            try:
                _blob_path(cache_directory, blob).unlink(missing_ok=True)
            except OSError as exc:
                raise MediaError("media_prune_file_failed") from exc
            session.delete(blob)
        session.flush()
    return MediaPruneResult(
        apply=apply,
        blob_count=len(selected),
        byte_count=sum(blob.byte_size for blob in selected),
        sha256s=tuple(blob.sha256 for blob in selected),
    )

from __future__ import annotations

from pathlib import Path

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from bangumi_local.adapters.steam import SteamDataError, SteamStoreMedia, fetch_store_media
from bangumi_local.db.models import Base, LibraryEntry, MediaBinding, MediaSource, SourceAccount
from bangumi_local.domain.media import CachedMedia
from bangumi_local.services.media import MediaError, normalize_remote_image_url
from bangumi_local.services.steam_covers import (
    SteamCoverTarget,
    fetch_steam_cover,
    persist_steam_cover,
    prepare_steam_cover_completion,
    steam_store_cover_references,
)


def _session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{(tmp_path / 'covers.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    account = SourceAccount(
        source="steam",
        external_account_id="100",
        account_name=None,
        config_json="{}",
        first_seen_at="2026-01-01T00:00:00Z",
        last_seen_at="2026-01-01T00:00:00Z",
    )
    session.add(account)
    session.flush()
    for app_id in ("70", "80"):
        session.add(
            LibraryEntry(
                source_account_id=account.id,
                external_id=app_id,
                work_id=None,
                title_observed=f"App {app_id}",
                localized_titles_json="{}",
                ownership_scope="owned",
                installed=False,
                playtime_minutes=0,
                last_played_at=None,
                metadata_source="test",
                metadata_json="{}",
                source_hash="0" * 64,
                match_status="unmatched",
                match_reason=None,
                match_updated_at=None,
                first_seen_at="2026-01-01T00:00:00Z",
                last_seen_at="2026-01-01T00:00:00Z",
            )
        )
    session.commit()
    return session


def _cached(tmp_path: Path, name: str = "cover.jpg") -> CachedMedia:
    target = tmp_path / name
    target.write_bytes(b"image")
    return CachedMedia("a" * 64, name, "image/jpeg", 5, 600, 900)


def test_steam_store_media_and_cachebuster_normalization() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "70": {
                        "success": True,
                        "data": {
                            "name": "Half-Life",
                            "header_image": "https://shared.akamai.steamstatic.com/steam/apps/70/header.jpg?t=123",
                            "capsule_image": "https://cdn.akamai.steamstatic.com/steam/apps/70/capsule.jpg",
                        },
                    }
                },
            )
        )
    )
    media = fetch_store_media("70", client=client)
    client.close()
    assert media is not None and media.title == "Half-Life"
    references = steam_store_cover_references(media)
    assert [item.variant for item in references] == [
        "library_portrait_2x",
        "library_portrait",
        "header",
        "capsule",
    ]
    assert references[2].remote_url is not None
    assert "?" not in references[2].remote_url
    assert normalize_remote_image_url(
        "https://shared.akamai.steamstatic.com/steam/apps/70/header.jpg?t=123",
        "steam",
    ).endswith("header.jpg")
    try:
        normalize_remote_image_url(
            "https://shared.akamai.steamstatic.com/steam/apps/70/header.jpg?token=x",
            "steam",
        )
    except MediaError as exc:
        assert str(exc) == "media_url_query_rejected"
    else:
        raise AssertionError("unsafe Steam query accepted")


def test_prepare_only_entries_with_effective_placeholder(tmp_path: Path) -> None:
    session = _session(tmp_path)
    entry = session.scalar(select(LibraryEntry).where(LibraryEntry.external_id == "70"))
    assert entry is not None
    source = MediaSource(
        id="source-70",
        provider="steam",
        external_id="70",
        variant="library_portrait",
        locale="",
        origin="remote",
        remote_url="https://cdn.akamai.steamstatic.com/steam/apps/70/library_600x900.jpg",
        logical_locator_json="{}",
        status="cached",
        current_blob_sha256="a" * 64,
        observed_at="2026-01-01T00:00:00Z",
        failure_count=0,
    )
    from bangumi_local.db.models import MediaBlob

    cached = _cached(tmp_path)
    session.add(
        MediaBlob(
            sha256=cached.sha256,
            storage_relpath=cached.storage_relpath,
            mime_type=cached.mime_type,
            byte_size=cached.byte_size,
            width=cached.width,
            height=cached.height,
            created_at="2026-01-01T00:00:00Z",
        )
    )
    session.add(source)
    session.flush()
    session.add(
        MediaBinding(
            id="binding-70",
            media_source_id=source.id,
            library_entry_id=entry.id,
            work_id=None,
            rating_queue_item_id=None,
            discovery_candidate_id=None,
            role="cover",
            priority=90,
            pinned_blob_sha256=None,
            first_observed_at="2026-01-01T00:00:00Z",
            last_observed_at="2026-01-01T00:00:00Z",
        )
    )
    session.commit()

    prepared = prepare_steam_cover_completion(
        session,
        tmp_path,
        account_id=None,
        app_ids=None,
        all_missing=True,
        force_refresh=False,
        max_items=250,
    )
    assert prepared.examined == 2
    assert prepared.already_available == 1
    assert [item.app_id for item in prepared.targets] == ["80"]
    refreshed = prepare_steam_cover_completion(
        session,
        tmp_path,
        account_id=None,
        app_ids=("70",),
        all_missing=False,
        force_refresh=True,
        max_items=250,
    )
    assert [item.app_id for item in refreshed.targets] == ["70"]
    session.close()


def test_fetch_falls_back_and_persists_library_binding(tmp_path: Path) -> None:
    session = _session(tmp_path)
    entry = session.scalar(select(LibraryEntry).where(LibraryEntry.external_id == "70"))
    assert entry is not None
    calls: list[str] = []

    def store_fetcher(app_id: str, **_kwargs: object) -> SteamStoreMedia:
        return SteamStoreMedia(
            app_id=app_id,
            title="Half-Life",
            header_image="https://cdn.akamai.steamstatic.com/steam/apps/70/header.jpg",
            capsule_image=None,
        )

    cached = _cached(tmp_path, "remote.jpg")

    def downloader(reference, *_args, **_kwargs):
        calls.append(reference.variant)
        if reference.variant == "library_portrait_2x":
            raise MediaError("media_remote_missing")
        return cached

    outcome = fetch_steam_cover(
        SteamCoverTarget(entry.id, "70"),
        tmp_path,
        max_bytes=1024,
        timeout_seconds=1,
        max_retries=2,
        retry_base_seconds=0,
        sleep_fn=lambda _seconds: None,
        store_fetcher=store_fetcher,
        downloader=downloader,
    )
    assert outcome.status == "cached"
    assert outcome.reference is not None
    assert outcome.reference.variant == "library_portrait"
    assert calls == ["library_portrait_2x", "library_portrait"]
    persist_steam_cover(session, outcome)
    session.commit()
    binding = session.scalar(
        select(MediaBinding).where(MediaBinding.library_entry_id == entry.id)
    )
    assert binding is not None and binding.priority == 90
    assert session.get(MediaSource, binding.media_source_id).status == "cached"
    session.close()


def test_store_transport_error_is_sanitized() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: (_ for _ in ()).throw(httpx.ReadTimeout("x")))
    )
    try:
        fetch_store_media("70", client=client)
    except SteamDataError as exc:
        assert str(exc) == "steam_store_timeout"
    else:
        raise AssertionError("timeout was not converted")
    finally:
        client.close()

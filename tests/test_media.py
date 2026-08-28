from __future__ import annotations

import json
from pathlib import Path

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from bangumi_local.config import get_settings
from bangumi_local.db.models import Base, MediaBlob, MediaSource
from bangumi_local.domain.media import ImagePolicy, LocalMediaCandidate, MediaReference
from bangumi_local.services.media import (
    MediaError,
    bangumi_image_references,
    cache_local_media,
    download_remote_media,
    media_status,
    normalize_remote_image_url,
    prune_media_cache,
    register_media_references,
    scan_steam_librarycache,
    verify_media_cache,
)


def _session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{(tmp_path / 'media.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    return Session(engine)


def _png(width: int = 2, height: int = 3) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + width.to_bytes(4, "big") + height.to_bytes(4, "big")


def test_media_settings_and_policy_are_portable(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BLD_MEDIA_CACHE_DIRECTORY=portable-media\n"
        "BLD_IMAGE_POLICY=cache\n"
        "BLD_IMAGE_MAX_ITEM_BYTES=4096\n",
        encoding="utf-8",
    )
    settings = get_settings(env_file)
    assert settings.media_cache_directory == Path("portable-media")
    assert settings.image_policy == "cache"
    assert settings.image_max_item_bytes == 4096
    assert ImagePolicy.parse("METADATA") is ImagePolicy.METADATA


def test_steam_librarycache_scan_and_content_addressed_dedup(tmp_path: Path) -> None:
    steam_root = tmp_path / "Steam Root With Spaces"
    app = steam_root / "appcache" / "librarycache" / "70"
    nested = app / "hash-generation"
    nested.mkdir(parents=True)
    (app / "library_600x900.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (app / "header_schinese.jpg").write_bytes(_png())
    (nested / "library_header.jpg").write_bytes(_png())

    candidates = scan_steam_librarycache(steam_root, app_ids=("70",))
    assert {(item.reference.variant, item.reference.locale) for item in candidates} == {
        ("library_portrait", ""),
        ("header", "schinese"),
        ("library_header", ""),
    }
    assert all(str(steam_root) not in json.dumps(item.reference.logical_locator) for item in candidates)

    cache = tmp_path / "cache"
    first = cache_local_media(candidates[1], cache, max_bytes=1024)
    second = cache_local_media(candidates[2], cache, max_bytes=1024)
    assert first.sha256 == second.sha256
    assert first.storage_relpath == second.storage_relpath
    assert len(list(cache.rglob("*.png"))) == 1


def test_metadata_policy_registers_without_cache_and_cache_policy_deduplicates(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    references = bangumi_image_references(
        101,
        {
            "common": "https://lain.bgm.tv/pic/cover/common/a.jpg",
            "large": "https://lain.bgm.tv/pic/cover/l/a.jpg",
        },
    )
    none = register_media_references(session, references, policy="none")
    assert none.skipped == 2
    assert media_status(session).source_count == 0

    metadata = register_media_references(session, references, policy="metadata")
    assert metadata.sources_created == 2
    assert media_status(session).cached_source_count == 0

    local = tmp_path / "same.png"
    local.write_bytes(_png())
    cached = cache_local_media(
        LocalMediaCandidate(references[0], local), tmp_path / "cache", max_bytes=1024
    )
    cached_map = {reference.key: cached for reference in references}
    result = register_media_references(
        session, references, policy="cache", cached=cached_map
    )
    session.commit()
    assert result.cached == 2
    assert session.scalar(select(MediaBlob).limit(1)) is not None
    assert session.scalar(select(MediaBlob.sha256).distinct()) == cached.sha256
    assert media_status(session).blob_count == 1
    assert all(
        str(tmp_path) not in source.logical_locator_json
        for source in session.scalars(select(MediaSource))
    )
    session.close()


def test_remote_download_rejects_untrusted_urls_and_redirects(tmp_path: Path) -> None:
    with_credential = "https://secret@example.com/image.jpg"
    for url in (
        "http://lain.bgm.tv/image.jpg",
        "https://example.com/image.jpg",
        "https://lain.bgm.tv/image.jpg?token=private",
        with_credential,
    ):
        try:
            normalize_remote_image_url(url, "bangumi")
        except MediaError as exc:
            assert str(exc).startswith("media_")
            assert "private" not in str(exc)
        else:
            raise AssertionError("unsafe URL accepted")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start.jpg":
            return httpx.Response(302, headers={"location": "https://example.com/escape.jpg"})
        return httpx.Response(200, content=_png())

    reference = MediaReference(
        provider="bangumi",
        external_id="101",
        variant="common",
        origin="remote",
        remote_url="https://lain.bgm.tv/start.jpg",
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        download_remote_media(
            reference, tmp_path / "cache", max_bytes=1024, client=client
        )
    except MediaError as exc:
        assert str(exc) == "media_host_rejected"
    else:
        raise AssertionError("unsafe redirect accepted")
    client.close()

    success_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "image/png"}, content=_png(4, 5)
            )
        )
    )
    cached = download_remote_media(
        MediaReference(
            provider="bangumi",
            external_id="101",
            variant="common",
            origin="remote",
            remote_url="https://lain.bgm.tv/image.jpg",
        ),
        tmp_path / "cache",
        max_bytes=1024,
        client=success_client,
    )
    success_client.close()
    assert (cached.mime_type, cached.width, cached.height) == ("image/png", 4, 5)

    oversized_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=_png(20_000, 2),
            )
        )
    )
    try:
        download_remote_media(
            MediaReference(
                provider="bangumi",
                external_id="101",
                variant="large",
                origin="remote",
                remote_url="https://lain.bgm.tv/image-large.png",
            ),
            tmp_path / "cache",
            max_bytes=1024,
            client=oversized_client,
        )
    except MediaError as exc:
        assert str(exc) == "media_dimensions_invalid"
    else:
        raise AssertionError("unsafe image dimensions accepted")
    oversized_client.close()


def test_verify_and_prune_are_dry_run_by_default(tmp_path: Path) -> None:
    session = _session(tmp_path)
    cache = tmp_path / "cache"
    source_file = tmp_path / "source.png"
    source_file.write_bytes(_png())
    candidate = LocalMediaCandidate(
        MediaReference("steam", "70", "header", "steam_local"), source_file
    )
    materialized = cache_local_media(candidate, cache, max_bytes=1024)
    reference = MediaReference(
        provider="steam",
        external_id="70",
        variant="header",
        origin="steam_local",
        logical_locator={"kind": "steam_librarycache", "app_id": "70", "filename": "header.jpg"},
    )
    register_media_references(
        session,
        (reference,),
        policy="cache",
        cached={reference.key: materialized},
    )
    session.commit()
    assert verify_media_cache(session, cache) == ()

    preview = prune_media_cache(session, cache, max_bytes=0)
    assert not preview.apply and preview.blob_count == 1
    assert (cache / materialized.storage_relpath).exists()
    applied = prune_media_cache(session, cache, max_bytes=0, apply=True)
    session.commit()
    assert applied.apply and applied.sha256s == (materialized.sha256,)
    assert not (cache / materialized.storage_relpath).exists()
    assert media_status(session).blob_count == 0
    session.close()

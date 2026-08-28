from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from conftest import make_remote_collection
from bangumi_local.adapters.bangumi import BangumiAPIError
from bangumi_local.db.models import Base, ChangePlanItem, RemoteOperation
from bangumi_local.db.repositories import local_snapshot
from bangumi_local.db.session import session_scope
from bangumi_local.domain.models import RemoteCollection
from bangumi_local.domain.mutations import CollectionPatch
from bangumi_local.services.apply_plan import (
    _BatchAbort,
    _ItemFailure,
    _patch_with_verification,
    apply_reviewed_plan,
    preflight_plan,
)
from bangumi_local.services.plans import (
    create_bulk_tag_plan,
    create_recovery_plan,
    load_plan,
    review_plan,
)
from bangumi_local.services.pull import pull_collections, snapshot_from_remote


class _FakeClient:
    def __init__(self, remote: RemoteCollection) -> None:
        self.remote = remote
        self.patch_calls = 0

    def get_game_collection(self, subject_id: int) -> RemoteCollection:
        assert subject_id == self.remote.subject_id
        return self.remote

    def get_collection(self, subject_id: int) -> RemoteCollection:
        return self.get_game_collection(subject_id)

    def patch_collection_tags(self, subject_id: int, tags: tuple[str, ...]) -> None:
        assert subject_id == self.remote.subject_id
        self.patch_calls += 1
        self.remote = replace(self.remote, tags=tags)

    def patch_collection(self, subject_id: int, patch: CollectionPatch) -> None:
        values = patch.values
        if set(values) == {"tags"}:
            self.patch_collection_tags(subject_id, tuple(values["tags"]))  # type: ignore[arg-type]
            return
        replacements = {
            "status": self.remote.status.__class__(values.get("type", self.remote.status)),
            "rate": values.get("rate", self.remote.rate),
            "comment": values.get("comment", self.remote.comment),
            "private": values.get("private", self.remote.private),
            "tags": tuple(values.get("tags", self.remote.tags)),
        }
        self.patch_calls += 1
        self.remote = replace(self.remote, **replacements)


def _reviewed_plan(database_url: str, remote: RemoteCollection) -> str:
    with session_scope(database_url) as session:
        pull_collections(session, [remote])
    with session_scope(database_url) as session:
        stored = create_bulk_tag_plan(
            session,
            [remote],
            operation="add",
            selector={"mode": "ids", "ids": [remote.subject_id]},
            detail_loader=lambda _subject_id: (),
            tag="Galgame",
        )
        plan_id = stored.plan.id
    with session_scope(database_url) as session:
        review_plan(session, plan_id)
    return plan_id


def test_apply_backs_up_verifies_audits_and_creates_reverse(tmp_path: Path) -> None:
    database_path = tmp_path / "apply.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    remote = make_remote_collection(tags=("RPG",))
    plan_id = _reviewed_plan(database_url, remote)
    client = _FakeClient(remote)

    preflight = preflight_plan(database_url, client, plan_id)  # type: ignore[arg-type]
    result = apply_reviewed_plan(
        database_url,
        client,  # type: ignore[arg-type]
        plan_id,
        preflight,
        backup_directory=tmp_path / "backups",
        write_delay_seconds=0,
        max_retries=1,
        retry_base_seconds=0.01,
        sleep_fn=lambda _seconds: None,
    )

    assert result.status == "applied"
    assert result.applied == 1 and result.reverse_plan_id is not None
    assert result.backup_path.is_file()
    assert client.patch_calls == 1
    with session_scope(database_url) as session:
        item = session.scalar(select(ChangePlanItem).where(ChangePlanItem.plan_id == plan_id))
        assert item is not None and item.item_status == "applied"
        assert local_snapshot(session, item.subject_id).tags == ("Galgame", "RPG")
        assert session.scalar(select(func.count()).select_from(RemoteOperation)) == 1
        operation = session.scalar(select(RemoteOperation))
        assert operation is not None and operation.status == "applied"
        reverse = load_plan(session, result.reverse_plan_id)
        assert reverse.plan.status == "draft"
        assert reverse.planned[0].before_tags == ("RPG", "Galgame")
        assert reverse.planned[0].after_tags == ("RPG",)

    with session_scope(database_url) as session:
        review_plan(session, result.reverse_plan_id)
    reverse_preflight = preflight_plan(
        database_url, client, result.reverse_plan_id  # type: ignore[arg-type]
    )
    reverse_result = apply_reviewed_plan(
        database_url,
        client,  # type: ignore[arg-type]
        result.reverse_plan_id,
        reverse_preflight,
        backup_directory=tmp_path / "backups",
        write_delay_seconds=0,
        max_retries=1,
        retry_base_seconds=0.01,
        sleep_fn=lambda _seconds: None,
    )
    assert reverse_result.status == "applied"
    assert client.remote.tags == ("RPG",)
    with session_scope(database_url) as session:
        item = session.scalar(
            select(ChangePlanItem).where(ChangePlanItem.plan_id == result.reverse_plan_id)
        )
        assert item is not None
        assert local_snapshot(session, item.subject_id).tags == ("RPG",)


def test_preflight_stale_never_calls_patch(tmp_path: Path) -> None:
    database_path = tmp_path / "stale.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    remote = make_remote_collection(rate=8, tags=("RPG",))
    plan_id = _reviewed_plan(database_url, remote)
    client = _FakeClient(replace(remote, rate=9))

    preflight = preflight_plan(database_url, client, plan_id)  # type: ignore[arg-type]

    assert preflight.will_modify == ()
    assert preflight.unchanged[0].reason == "stale_remote"
    assert client.patch_calls == 0


def test_mixed_preflight_stale_is_persisted_and_reverse_contains_only_success(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "mixed-stale.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    remotes = [
        make_remote_collection(subject_id=101, tags=("RPG",)),
        make_remote_collection(subject_id=102, tags=("RPG",)),
    ]
    with session_scope(database_url) as session:
        pull_collections(session, remotes)
    with session_scope(database_url) as session:
        stored = create_bulk_tag_plan(
            session,
            remotes,
            operation="add",
            selector={"mode": "ids", "ids": [101, 102]},
            detail_loader=lambda _subject_id: (),
            tag="Galgame",
        )
        plan_id = stored.plan.id
        review_plan(session, plan_id)

    class MappingClient:
        def __init__(self) -> None:
            self.remotes = {101: remotes[0], 102: replace(remotes[1], rate=9)}
            self.patched: list[int] = []

        def get_collection(self, subject_id: int) -> RemoteCollection:
            return self.remotes[subject_id]

        def patch_collection(self, subject_id: int, patch: CollectionPatch) -> None:
            self.patched.append(subject_id)
            self.remotes[subject_id] = replace(
                self.remotes[subject_id], tags=tuple(patch.values["tags"])  # type: ignore[arg-type]
            )

    client = MappingClient()
    preflight = preflight_plan(database_url, client, plan_id)  # type: ignore[arg-type]
    assert [item.subject_id for item in preflight.will_modify] == [101]
    assert any(item.subject_id == 102 and item.reason == "stale_remote" for item in preflight.unchanged)

    result = apply_reviewed_plan(
        database_url,
        client,  # type: ignore[arg-type]
        plan_id,
        preflight,
        backup_directory=tmp_path / "backups",
        write_delay_seconds=0,
        max_retries=0,
        retry_base_seconds=0.01,
        sleep_fn=lambda _seconds: None,
    )
    assert result.status == "partial"
    assert (result.applied, result.stale, result.failed, result.pending) == (1, 1, 0, 0)
    assert client.patched == [101]
    assert result.reverse_plan_id is not None
    with session_scope(database_url) as session:
        reverse = load_plan(session, result.reverse_plan_id)
        assert [item.subject_id for item in reverse.planned] == [101]


def test_apply_is_serial_delayed_and_backup_exists_before_first_patch(tmp_path: Path) -> None:
    database_path = tmp_path / "serial.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    backup_directory = tmp_path / "backups"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    remotes = [
        make_remote_collection(subject_id=101, tags=("RPG",)),
        make_remote_collection(subject_id=102, tags=("RPG",)),
    ]
    with session_scope(database_url) as session:
        pull_collections(session, remotes)
    with session_scope(database_url) as session:
        stored = create_bulk_tag_plan(
            session,
            remotes,
            operation="add",
            selector={"mode": "ids", "ids": [101, 102]},
            detail_loader=lambda _subject_id: (),
            tag="Galgame",
        )
        plan_id = stored.plan.id
        review_plan(session, plan_id)

    class OrderedClient:
        def __init__(self) -> None:
            self.remotes = {item.subject_id: item for item in remotes}
            self.patched: list[int] = []

        def get_collection(self, subject_id: int) -> RemoteCollection:
            return self.remotes[subject_id]

        def patch_collection(self, subject_id: int, patch: CollectionPatch) -> None:
            assert list(backup_directory.glob("*.sqlite3"))
            self.patched.append(subject_id)
            self.remotes[subject_id] = replace(
                self.remotes[subject_id], tags=tuple(patch.values["tags"])  # type: ignore[arg-type]
            )

    client = OrderedClient()
    preflight = preflight_plan(database_url, client, plan_id)  # type: ignore[arg-type]
    sleeps: list[float] = []
    result = apply_reviewed_plan(
        database_url,
        client,  # type: ignore[arg-type]
        plan_id,
        preflight,
        backup_directory=backup_directory,
        write_delay_seconds=0.5,
        max_retries=0,
        retry_base_seconds=0.01,
        sleep_fn=sleeps.append,
    )
    assert result.status == "applied"
    assert client.patched == [101, 102]
    assert sleeps == [0.5]


def test_timeout_verifies_success_before_any_retry() -> None:
    remote = make_remote_collection(tags=("RPG",))
    before = snapshot_from_remote(remote)
    intended = before.replacing({"tags": ("RPG", "Galgame")})

    class TimeoutAfterWrite(_FakeClient):
        def patch_collection_tags(self, subject_id: int, tags: tuple[str, ...]) -> None:
            self.patch_calls += 1
            self.remote = replace(self.remote, tags=tags)
            raise BangumiAPIError("timed out", timed_out=True)

    client = TimeoutAfterWrite(remote)
    actual, attempts, _status = _patch_with_verification(
        client,  # type: ignore[arg-type]
        subject_id=remote.subject_id,
        before=before,
        intended=intended,
        patch=CollectionPatch({"tags": ("RPG", "Galgame")}),
        max_retries=4,
        retry_base_seconds=0.01,
        sleep_fn=lambda _seconds: None,
    )

    assert actual == intended
    assert attempts == 1
    assert client.patch_calls == 1


def test_429_verifies_before_then_retries_with_retry_after() -> None:
    remote = make_remote_collection(tags=("RPG",))
    before = snapshot_from_remote(remote)
    intended = before.replacing({"tags": ("RPG", "Galgame")})
    sleeps: list[float] = []

    class RateLimitedOnce(_FakeClient):
        def patch_collection_tags(self, subject_id: int, tags: tuple[str, ...]) -> None:
            self.patch_calls += 1
            if self.patch_calls == 1:
                raise BangumiAPIError(
                    "rate limited", status_code=429, retry_after_seconds=0.25
                )
            self.remote = replace(self.remote, tags=tags)

    client = RateLimitedOnce(remote)
    actual, attempts, _status = _patch_with_verification(
        client,  # type: ignore[arg-type]
        subject_id=remote.subject_id,
        before=before,
        intended=intended,
        patch=CollectionPatch({"tags": ("RPG", "Galgame")}),
        max_retries=4,
        retry_base_seconds=0.01,
        sleep_fn=sleeps.append,
    )
    assert actual == intended
    assert attempts == 2
    assert client.patch_calls == 2
    assert sleeps == [0.25]


def test_401_aborts_without_retry() -> None:
    remote = make_remote_collection(tags=("RPG",))
    before = snapshot_from_remote(remote)
    intended = before.replacing({"tags": ("RPG", "Galgame")})

    class Unauthorized(_FakeClient):
        def patch_collection_tags(self, subject_id: int, tags: tuple[str, ...]) -> None:
            self.patch_calls += 1
            raise BangumiAPIError("unauthorized", status_code=401)

    client = Unauthorized(remote)
    with pytest.raises(_BatchAbort):
        _patch_with_verification(
            client,  # type: ignore[arg-type]
            subject_id=remote.subject_id,
            before=before,
            intended=intended,
            patch=CollectionPatch({"tags": ("RPG", "Galgame")}),
            max_retries=4,
            retry_base_seconds=0.01,
            sleep_fn=lambda _seconds: None,
        )
    assert client.patch_calls == 1


@pytest.mark.parametrize("status_code", (400, 404))
def test_non_retryable_client_error_fails_only_the_item(status_code: int) -> None:
    remote = make_remote_collection(tags=("RPG",))
    before = snapshot_from_remote(remote)
    intended = before.replacing({"tags": ("RPG", "Galgame")})

    class ClientError(_FakeClient):
        def patch_collection_tags(self, subject_id: int, tags: tuple[str, ...]) -> None:
            self.patch_calls += 1
            raise BangumiAPIError("client error", status_code=status_code)

    client = ClientError(remote)
    with pytest.raises(_ItemFailure) as caught:
        _patch_with_verification(
            client,  # type: ignore[arg-type]
            subject_id=remote.subject_id,
            before=before,
            intended=intended,
            patch=CollectionPatch({"tags": ("RPG", "Galgame")}),
            max_retries=4,
            retry_base_seconds=0.01,
            sleep_fn=lambda _seconds: None,
        )
    assert caught.value.http_status == status_code
    assert caught.value.attempts == 1
    assert client.patch_calls == 1


def test_5xx_verifies_before_retrying_with_exponential_backoff() -> None:
    remote = make_remote_collection(tags=("RPG",))
    before = snapshot_from_remote(remote)
    intended = before.replacing({"tags": ("RPG", "Galgame")})
    sleeps: list[float] = []

    class ServerErrorOnce(_FakeClient):
        def patch_collection_tags(self, subject_id: int, tags: tuple[str, ...]) -> None:
            self.patch_calls += 1
            if self.patch_calls == 1:
                raise BangumiAPIError("server error", status_code=503)
            self.remote = replace(self.remote, tags=tags)

    client = ServerErrorOnce(remote)
    actual, attempts, status = _patch_with_verification(
        client,  # type: ignore[arg-type]
        subject_id=remote.subject_id,
        before=before,
        intended=intended,
        patch=CollectionPatch({"tags": ("RPG", "Galgame")}),
        max_retries=4,
        retry_base_seconds=0.5,
        sleep_fn=sleeps.append,
    )
    assert actual == intended
    assert attempts == 2 and status == 204
    assert sleeps == [0.5]


def test_unexpected_case_expansion_can_be_recovered_with_a_new_plan(tmp_path: Path) -> None:
    database_path = tmp_path / "recovery.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    remote = make_remote_collection(tags=())
    source_plan_id = _reviewed_plan(database_url, remote)

    class CaseExpandingClient(_FakeClient):
        def patch_collection_tags(self, subject_id: int, tags: tuple[str, ...]) -> None:
            self.patch_calls += 1
            expanded = (*tags, "galgame") if tags else ()
            self.remote = replace(self.remote, tags=expanded)

    client = CaseExpandingClient(remote)
    source_preflight = preflight_plan(
        database_url, client, source_plan_id  # type: ignore[arg-type]
    )
    source_result = apply_reviewed_plan(
        database_url,
        client,  # type: ignore[arg-type]
        source_plan_id,
        source_preflight,
        backup_directory=tmp_path / "backups",
        write_delay_seconds=0,
        max_retries=0,
        retry_base_seconds=0.01,
        sleep_fn=lambda _seconds: None,
    )
    assert source_result.status == "failed"
    assert source_result.reverse_plan_id is None
    assert client.remote.tags == ("Galgame", "galgame")

    with session_scope(database_url) as session:
        recovery = create_recovery_plan(session, source_plan_id, [client.remote])
        recovery_id = recovery.plan.id
        assert recovery.plan.kind == "recovery"
        assert recovery.planned[0].before_tags == ("Galgame", "galgame")
        assert recovery.planned[0].after_tags == ()
    with session_scope(database_url) as session:
        review_plan(session, recovery_id)
    recovery_preflight = preflight_plan(
        database_url, client, recovery_id  # type: ignore[arg-type]
    )
    recovery_result = apply_reviewed_plan(
        database_url,
        client,  # type: ignore[arg-type]
        recovery_id,
        recovery_preflight,
        backup_directory=tmp_path / "backups",
        write_delay_seconds=0,
        max_retries=0,
        retry_base_seconds=0.01,
        sleep_fn=lambda _seconds: None,
    )
    assert recovery_result.status == "applied"
    assert client.remote.tags == ()

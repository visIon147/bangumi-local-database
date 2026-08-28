from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from typer.testing import CliRunner

from conftest import make_remote_collection
from bangumi_local.cli import app
from bangumi_local.db.models import Base
from bangumi_local.db.session import create_database_engine, session_scope
from bangumi_local.domain.models import RemoteGame
from bangumi_local.db.repositories import local_snapshot
from bangumi_local.services.pull import pull_collections


def test_status_json_and_unicode_list_output(tmp_path: Path) -> None:
    database_path = tmp_path / "cli.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    remote = make_remote_collection()
    unicode_remote = replace(
        remote,
        subject=RemoteGame(
            subject_id=remote.subject_id,
            title_original="D.C.5 ～ダ・カーポ5～",
            title_cn=None,
            summary=remote.game.summary,
            release_date=remote.game.release_date,
            cover_url=remote.game.cover_url,
        ),
    )
    with session_scope(database_url) as session:
        pull_collections(session, [unicode_remote])

    runner = CliRunner()
    environment = {"BGV_DATABASE_URL": database_url}
    status_result = runner.invoke(app, ["status", "--json"], env=environment)
    list_result = runner.invoke(app, ["list"], env=environment)

    assert status_result.exit_code == 0
    payload = json.loads(status_result.stdout)
    assert payload["remote_source"] == "shadow-cache"
    assert payload["counts"]["clean"] == 1
    assert list_result.exit_code == 0
    assert "D.C.5 ～ダ・カーポ5～" in list_result.stdout


def test_collection_edit_cli_is_local_only_and_bld_env_has_priority(tmp_path: Path) -> None:
    database_path = tmp_path / "edit-cli.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    remote = make_remote_collection(private=True)
    with session_scope(database_url) as session:
        pull_collections(session, [remote])

    result = CliRunner().invoke(
        app,
        ["collection", "edit", str(remote.subject_id), "--rating", "9", "--public"],
        env={
            "BLD_DATABASE_URL": database_url,
            "BGV_DATABASE_URL": "sqlite:///./must-not-win.sqlite3",
        },
    )

    assert result.exit_code == 0
    assert "Bangumi requests performed: 0" in result.stdout
    with session_scope(database_url) as session:
        snapshot = local_snapshot(session, remote.subject_id)
        assert snapshot.rating == 9
        assert snapshot.is_private is False

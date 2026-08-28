from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from bangumi_local.db.session import create_database_engine
from bangumi_local.services.migrations import upgrade_database_safely


def test_alembic_migration_creates_phase5_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "migrated.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url

    command.upgrade(config, "head")
    command.check(config)

    engine = create_engine(database_url)
    inspector = inspect(engine)
    expected = {
        "works",
        "bangumi_subjects",
        "bangumi_collection_states",
        "game_profiles",
        "tags",
        "work_tags",
        "work_links",
        "sync_shadows",
        "sync_conflicts",
        "change_plans",
        "change_plan_items",
        "plan_apply_runs",
        "remote_operations",
        "source_accounts",
        "library_import_runs",
        "library_entries",
        "library_collections",
        "library_entry_collections",
        "library_match_candidates",
            "library_match_reviews",
            "rating_review_states",
            "rating_queue_sessions",
            "rating_queue_items",
            "rating_review_events",
            "discovery_sessions",
            "discovery_candidates",
            "discovery_review_states",
            "discovery_review_events",
            "media_blobs",
            "media_sources",
            "media_renditions",
            "media_bindings",
            "ui_jobs",
                "ui_job_events",
                "ui_job_plan_links",
                "plan_confirmation_nonces",
        }
    assert expected <= set(inspector.get_table_names())
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0013_workspace_history"
        review_columns = {
            column["name"] for column in inspector.get_columns("library_match_reviews")
        }
        assert {"plan_id", "score", "evidence_json"} <= review_columns
    assert all(
        "token" not in column["name"].lower()
        for table_name in inspector.get_table_names()
        for column in inspector.get_columns(table_name)
    )
    engine.dispose()

    application_engine = create_database_engine(database_url)
    with application_engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    application_engine.dispose()


def test_phase4_migration_preserves_phase1_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "phase1.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "0001_phase1")

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO games "
                "(id, bgm_subject_id, title, created_at, updated_at) "
                "VALUES (1, 101, 'Existing Game', '2025-01-01T00:00:00Z', "
                "'2025-01-01T00:00:00Z')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO collection_states "
                "(game_id, bgm_collection_type, rating, comment, is_private, "
                "confidence, completion, local_updated_at) "
                "VALUES (1, 2, 8, 'kept', 0, 'unknown', 'unknown', "
                "'2025-01-01T00:00:00Z')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sync_shadows "
                "(game_id, remote_snapshot_json, remote_hash, synced_at) "
                "VALUES (1, :snapshot, 'placeholder', '2025-01-01T00:00:00Z')"
            ),
            {
                "snapshot": json.dumps(
                    {
                        "comment": "kept",
                        "private": False,
                        "rate": 8,
                        "tags": [],
                        "type": 2,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT title FROM works WHERE id=1")).scalar_one() == "Existing Game"
        assert connection.execute(
            text("SELECT remote_snapshot_json FROM sync_shadows WHERE subject_id=101")
        ).scalar_one().startswith('{"comment":"kept"')
        assert connection.execute(text("SELECT kind FROM works WHERE id=1")).scalar_one() == "game"
        assert connection.execute(text("SELECT work_id FROM bangumi_subjects WHERE subject_id=101")).scalar_one() == 1
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0013_workspace_history"
        assert connection.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    assert "sync_conflicts" in inspect(engine).get_table_names()
    assert "change_plans" in inspect(engine).get_table_names()
    engine.dispose()


def test_safe_upgrade_creates_backup_manifest_and_verifies_head(tmp_path: Path) -> None:
    database_path = tmp_path / "safe-upgrade.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "0003_phase3")

    result = upgrade_database_safely(database_url, tmp_path / "backups")

    assert result.from_revision == "0003_phase3"
    assert result.to_revision == "0013_workspace_history"
    assert result.backup_path.is_file()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "verified"
    assert manifest["foreign_key_violations"] == []
    assert manifest["before"]["games"]["rows"] == manifest["after"]["works"]["rows"]


def test_safe_upgrade_supports_a_new_empty_sqlite_file(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"

    result = upgrade_database_safely(database_url, tmp_path / "backups")

    assert result.from_revision is None
    assert result.to_revision == "0013_workspace_history"
    assert result.foreign_key_violations == ()
    assert result.backup_path.is_file()
    engine = create_engine(database_url)
    assert "library_entries" in inspect(engine).get_table_names()
    engine.dispose()


def test_safe_upgrade_uses_packaged_migrations_outside_source_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_directory = tmp_path / "installed workspace"
    work_directory.mkdir()
    monkeypatch.chdir(work_directory)
    database_path = work_directory / "data/fresh.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"

    result = upgrade_database_safely(database_url, work_directory / "backups")

    assert result.to_revision == "0013_workspace_history"


def test_workspace_migration_backfills_job_plan_links(tmp_path: Path) -> None:
    database_path = tmp_path / "workspace-backfill.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "0012_ui_jobs")
    engine = create_engine(database_url)
    now = "2026-08-28T00:00:00Z"
    plan_id = "11111111-1111-1111-1111-111111111111"
    job_id = "22222222-2222-2222-2222-222222222222"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO change_plans "
                "(id, format_version, kind, operation, selector_json, summary_json, content_hash, "
                "status, created_by, created_at) VALUES "
                "(:id, 4, 'steam_match', 'match', '{}', '{}', :hash, 'draft', 'manual', :now)"
            ),
            {"id": plan_id, "hash": "0" * 64, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO ui_jobs "
                "(id, kind, capability, status, config_json, result_json, progress_current, "
                "progress_total, phase, created_at, finished_at) VALUES "
                "(:id, 'steam_match_plan', 'remote_read', 'succeeded', '{}', :result, 1, 1, "
                "'completed', :now, :now)"
            ),
            {"id": job_id, "result": json.dumps({"plan_id": plan_id}), "now": now},
        )
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT job_id, plan_id, relation FROM ui_job_plan_links")
        ).one()
        assert row == (job_id, plan_id, "created")
    engine.dispose()
    assert database_path.is_file()


def test_phase4_downgrade_is_lossless_when_database_is_still_phase3_compatible(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "safe-downgrade.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "0001_phase1")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO games (id, bgm_subject_id, title, bgm_url, created_at, updated_at) "
                "VALUES (1, 101, 'Downgrade Game', 'https://bgm.tv/subject/101', "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO collection_states "
                "(game_id, bgm_collection_type, rating, is_private, confidence, completion, local_updated_at) "
                "VALUES (1, 2, 8, 0, 'confirmed', 'completed', '2026-01-01T00:00:00Z')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sync_shadows (game_id, remote_snapshot_json, remote_hash, synced_at) "
                "VALUES (1, '{}', 'hash', '2026-01-01T00:00:00Z')"
            )
        )
    engine.dispose()
    command.upgrade(config, "head")
    command.downgrade(config, "0003_phase3")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT title FROM games")).scalar_one() == "Downgrade Game"
        assert connection.execute(text("SELECT confidence FROM collection_states")).scalar_one() == "confirmed"
        assert connection.execute(text("SELECT game_id FROM sync_shadows")).scalar_one() == 1
        assert connection.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0003_phase3"
    engine.dispose()


def test_phase4_downgrade_refuses_new_subject_types_before_mutating_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "refused-downgrade.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO works (id, kind, title, created_at, updated_at) "
                "VALUES (1, 'anime', 'Anime', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="Cannot downgrade"):
        command.downgrade(config, "0003_phase3")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert "format_version" in {
        column["name"] for column in inspector.get_columns("change_plans")
    }
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0005_generic_plan_v2"
    engine.dispose()

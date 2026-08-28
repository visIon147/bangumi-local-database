from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import make_url

from bangumi_local.services.backups import BackupError, backup_sqlite_database


class MigrationSafetyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationResult:
    backup_path: Path
    manifest_path: Path
    from_revision: str | None
    to_revision: str
    foreign_key_violations: tuple[tuple[object, ...], ...]


def _table_manifest(database_url: str) -> dict[str, dict[str, object]]:
    engine = create_engine(database_url)
    metadata = MetaData()
    result: dict[str, dict[str, object]] = {}
    try:
        inspector = inspect(engine)
        for table_name in sorted(inspector.get_table_names()):
            if table_name == "alembic_version":
                continue
            table = Table(table_name, metadata, autoload_with=engine)
            primary_keys = list(table.primary_key.columns)
            statement = select(table)
            if primary_keys:
                statement = statement.order_by(*primary_keys)
            digest = hashlib.sha256()
            count = 0
            with engine.connect() as connection:
                for row in connection.execute(statement).mappings():
                    encoded = json.dumps(
                        dict(row),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                    digest.update(encoded)
                    digest.update(b"\n")
                    count += 1
            result[table_name] = {"rows": count, "sha256": digest.hexdigest()}
    finally:
        engine.dispose()
    return result


def _revision(database_url: str) -> str | None:
    engine = create_engine(database_url)
    try:
        if "alembic_version" not in inspect(engine).get_table_names():
            return None
        with engine.connect() as connection:
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()


def _v1_plan_hashes(database_url: str) -> dict[str, str]:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        if "change_plans" not in inspector.get_table_names():
            return {}
        columns = {
            column["name"] for column in inspector.get_columns("change_plans")
        }
        predicate = "WHERE format_version = 1" if "format_version" in columns else ""
        with engine.connect() as connection:
            rows = connection.execute(
                text(f"SELECT id, content_hash FROM change_plans {predicate} ORDER BY id")
            ).all()
        return {str(plan_id): str(content_hash) for plan_id, content_hash in rows}
    finally:
        engine.dispose()


def _validate_preserved_counts(
    before: dict[str, dict[str, object]], after: dict[str, dict[str, object]]
) -> None:
    mappings = {
        "games": "works",
        "collection_states": "bangumi_collection_states",
        "game_tags": "work_tags",
        "external_links": "work_links",
        "sync_shadows": "sync_shadows",
        "sync_conflicts": "sync_conflicts",
        "change_plans": "change_plans",
        "change_plan_items": "change_plan_items",
        "plan_apply_runs": "plan_apply_runs",
        "remote_operations": "remote_operations",
    }
    for old_name, new_name in mappings.items():
        if old_name not in before:
            continue
        if new_name not in after:
            raise MigrationSafetyError(f"Migrated table is missing: {new_name}")
        old_count = before[old_name]["rows"]
        new_count = after[new_name]["rows"]
        if old_count != new_count:
            raise MigrationSafetyError(
                f"Row-count mismatch after migration: {old_name}={old_count}, "
                f"{new_name}={new_count}"
            )
    if "games" in before:
        expected = before["games"]["rows"]
        for name in ("bangumi_subjects", "bangumi_collection_states", "game_profiles"):
            if name not in after or after[name]["rows"] != expected:
                actual = after.get(name, {}).get("rows", "missing")
                raise MigrationSafetyError(
                    f"Generic identity migration mismatch: games={expected}, {name}={actual}"
                )


def upgrade_database_safely(
    database_url: str,
    backup_directory: Path,
    *,
    alembic_ini: Path | None = None,
    target: str = "head",
) -> MigrationResult:
    url = make_url(database_url)
    if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
        raise MigrationSafetyError("Safe migration currently requires a file-backed SQLite database.")
    source_path = Path(url.database).expanduser().resolve()
    if source_path.exists() and not source_path.is_file():
        raise MigrationSafetyError(f"SQLite database path is not a file: {source_path}")
    if not source_path.exists():
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.touch(exist_ok=False)
    local_alembic_ini = Path("alembic.ini")
    source_root = Path(__file__).resolve().parents[3]
    source_alembic_ini = source_root / "alembic.ini"
    packaged_migration_directory = Path(__file__).resolve().parents[1] / "migrations"
    resolved_alembic_ini = (
        alembic_ini
        if alembic_ini is not None
        else (
            local_alembic_ini
            if local_alembic_ini.is_file()
            else (
                source_alembic_ini
                if source_alembic_ini.is_file()
                else packaged_migration_directory / "alembic.ini"
            )
        )
    )
    if not resolved_alembic_ini.is_file():
        raise MigrationSafetyError(
            f"Alembic configuration does not exist: {resolved_alembic_ini}"
        )

    before_revision = _revision(database_url)
    before_tables = _table_manifest(database_url)
    before_plan_hashes = _v1_plan_hashes(database_url)
    try:
        backup_path = backup_sqlite_database(
            database_url, backup_directory, label=f"before-migration-{target}"
        )
    except BackupError as exc:
        raise MigrationSafetyError(str(exc)) from exc

    manifest_path = backup_path.with_suffix(".manifest.json")
    manifest: dict[str, Any] = {
        "database": source_path.name,
        "backup": backup_path.name,
        "target": target,
        "before_revision": before_revision,
        "before": before_tables,
        "v1_plan_hashes_before": before_plan_hashes,
        "status": "backup-created",
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    config = Config(str(resolved_alembic_ini))
    if resolved_alembic_ini.parent.resolve() == packaged_migration_directory.resolve():
        config.set_main_option("script_location", str(packaged_migration_directory))
    elif resolved_alembic_ini.resolve() == source_alembic_ini.resolve():
        config.set_main_option("script_location", str(source_root / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, target)

    after_revision = _revision(database_url)
    after_tables = _table_manifest(database_url)
    after_plan_hashes = _v1_plan_hashes(database_url)
    _validate_preserved_counts(before_tables, after_tables)
    if before_plan_hashes != after_plan_hashes:
        raise MigrationSafetyError("Existing v1 plan IDs or content hashes changed during migration.")

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            violations = tuple(
                tuple(row) for row in connection.execute(text("PRAGMA foreign_key_check"))
            )
    finally:
        engine.dispose()
    if violations:
        raise MigrationSafetyError(
            f"Migration produced foreign-key violations: {violations!r}"
        )

    manifest.update(
        {
            "after_revision": after_revision,
            "after": after_tables,
            "v1_plan_hashes_after": after_plan_hashes,
            "foreign_key_violations": list(violations),
            "status": "verified",
        }
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if after_revision is None:
        raise MigrationSafetyError("Migration completed without an Alembic revision.")
    return MigrationResult(
        backup_path=backup_path,
        manifest_path=manifest_path,
        from_revision=before_revision,
        to_revision=after_revision,
        foreign_key_violations=violations,
    )

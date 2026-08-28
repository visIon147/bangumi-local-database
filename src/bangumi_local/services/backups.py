from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url


class BackupError(RuntimeError):
    pass


def backup_sqlite_database(database_url: str, directory: Path, *, label: str) -> Path:
    url = make_url(database_url)
    if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
        raise BackupError("Remote apply requires a file-backed SQLite database backup.")
    source_path = Path(url.database).expanduser().resolve()
    if not source_path.is_file():
        raise BackupError(f"SQLite database does not exist: {source_path}")
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = "".join(character for character in label if character.isalnum() or character in "-_")
    target = directory / f"bangumi-local-{safe_label}-{stamp}.sqlite3"
    with sqlite3.connect(source_path) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    return target

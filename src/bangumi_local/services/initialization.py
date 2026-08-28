from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import os
from pathlib import Path
import uuid


class InitializationError(ValueError):
    """Raised when safe first-run files cannot be initialized."""


@dataclass(frozen=True)
class InitializationResult:
    target_directory: Path
    created: tuple[Path, ...]
    skipped: tuple[Path, ...]


_TEMPLATES = (
    ("env.example", Path(".env")),
    ("steam.example.toml", Path("config/steam.toml")),
)


def initialize_user_directory(target_directory: Path) -> InitializationResult:
    target = target_directory.expanduser().resolve()
    if target.exists() and not target.is_dir():
        raise InitializationError(f"Target is not a directory: {target}")
    target.mkdir(parents=True, exist_ok=True)

    resource_root = files("bangumi_local.resources")
    created: list[Path] = []
    skipped: list[Path] = []
    for resource_name, relative_destination in _TEMPLATES:
        destination = target / relative_destination
        if destination.exists():
            skipped.append(relative_destination)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = resource_root.joinpath(resource_name).read_text(encoding="utf-8")
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        created.append(relative_destination)

    return InitializationResult(
        target_directory=target,
        created=tuple(created),
        skipped=tuple(skipped),
    )

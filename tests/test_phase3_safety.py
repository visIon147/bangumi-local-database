from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from bangumi_local.cli import app
from bangumi_local.db.models import Base


def test_secret_and_private_api_safety_guards() -> None:
    ignored = {
        line.strip()
        for line in Path(".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert ".env" in ignored
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in Path("src").rglob("*.py")
    )
    assert "/p1/" not in source
    assert all(
        "token" not in column.name.casefold()
        for table in Base.metadata.tables.values()
        for column in table.columns
    )


def test_apply_has_exact_id_confirmation_but_no_yes_flag() -> None:
    result = CliRunner().invoke(app, ["plan", "apply", "--help"])
    assert result.exit_code == 0
    assert "--confirm-plan-id" in result.stdout
    assert "--non-interactive" in result.stdout
    assert "--yes" not in result.stdout and "-y" not in result.stdout


def test_bulk_tag_cli_exposes_optional_subject_type() -> None:
    for command in ("bulk-add", "bulk-remove", "rename"):
        result = CliRunner().invoke(app, ["tags", command, "--help"])
        assert result.exit_code == 0
        assert "--subject-type" in result.stdout


def test_ui_serve_exposes_explicit_env_file() -> None:
    result = CliRunner().invoke(app, ["ui", "serve", "--help"])
    assert result.exit_code == 0
    assert "--env-file" in result.stdout

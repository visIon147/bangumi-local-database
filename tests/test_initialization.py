from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from bangumi_local.cli import app
from bangumi_local.services.initialization import initialize_user_directory


def test_initialize_user_directory_creates_safe_templates_idempotently(tmp_path: Path) -> None:
    target = tmp_path / "path with spaces"
    first = initialize_user_directory(target)

    assert first.created == (Path(".env"), Path("config/steam.toml"))
    assert first.skipped == ()
    env_text = (target / ".env").read_text(encoding="utf-8")
    assert "BANGUMI_ACCESS_TOKEN=\n" in env_text
    assert "STEAM_WEB_API_KEY=\n" in env_text
    assert "replace-with" not in env_text

    (target / ".env").write_text("USER_VALUE=keep\n", encoding="utf-8")
    second = initialize_user_directory(target)
    assert second.created == ()
    assert second.skipped == (Path(".env"), Path("config/steam.toml"))
    assert (target / ".env").read_text(encoding="utf-8") == "USER_VALUE=keep\n"


def test_init_cli_reports_created_and_skipped(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    runner = CliRunner()

    first = runner.invoke(app, ["init", "--target-directory", str(target)])
    second = runner.invoke(app, ["init", "--target-directory", str(target)])

    assert first.exit_code == 0
    assert "Summary: created=2 skipped=0" in first.stdout
    assert second.exit_code == 0
    assert "Summary: created=0 skipped=2" in second.stdout

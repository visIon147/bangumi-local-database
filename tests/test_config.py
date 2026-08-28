from __future__ import annotations

from pathlib import Path

from bangumi_local.config import get_settings


def test_settings_load_bangumi_values_from_dotenv(tmp_path: Path) -> None:
    secret = "".join(("local", "-", "only", "-", "credential"))
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                f"BANGUMI_ACCESS_TOKEN={secret}",
                "BANGUMI_USERNAME=tester",
                "BANGUMI_USER_AGENT=tester/bgm-game-vault/0.1 (tests)",
                "BGV_DATABASE_URL=sqlite:///./temporary.sqlite3",
            )
        ),
        encoding="utf-8",
    )

    settings = get_settings(env_file)
    token, username, user_agent = settings.require_bangumi()

    assert token.get_secret_value() == secret
    assert secret not in repr(settings)
    assert username == "tester"
    assert user_agent == "tester/bgm-game-vault/0.1 (tests)"
    assert settings.database_url == "sqlite:///./temporary.sqlite3"


def test_bld_environment_names_take_precedence_over_bgv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "BLD_DATABASE_URL=sqlite:///./new.sqlite3",
                "BGV_DATABASE_URL=sqlite:///./old.sqlite3",
                "BLD_PLAN_DIRECTORY=new-plans",
                "BGV_PLAN_DIRECTORY=old-plans",
                "BLD_BACKUP_DIRECTORY=new-backups",
                "BGV_BACKUP_DIRECTORY=old-backups",
            )
        ),
        encoding="utf-8",
    )
    settings = get_settings(env_file)
    assert settings.database_url == "sqlite:///./new.sqlite3"
    assert settings.plan_directory == Path("new-plans")
    assert settings.backup_directory == Path("new-backups")

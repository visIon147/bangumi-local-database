from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(ValueError):
    """Raised for missing or unsafe runtime configuration."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    bangumi_access_token: SecretStr | None = None
    bangumi_username: str | None = None
    bangumi_user_agent: str | None = None
    bangumi_base_url: str = "https://api.bgm.tv"
    bangumi_web_base_url: Literal[
        "https://bgm.tv", "https://bangumi.tv", "https://chii.in"
    ] = Field(
        default="https://bgm.tv",
        validation_alias=AliasChoices(
            "BLD_BANGUMI_WEB_BASE_URL", "BANGUMI_WEB_BASE_URL"
        ),
    )
    bangumi_request_timeout_seconds: float = Field(default=20.0, gt=0)
    database_url: str = Field(
        default="sqlite:///./data/gamevault.db",
        validation_alias=AliasChoices("BLD_DATABASE_URL", "BGV_DATABASE_URL", "DATABASE_URL"),
    )
    plan_directory: Path = Field(
        default=Path("plans"),
        validation_alias=AliasChoices("BLD_PLAN_DIRECTORY", "BGV_PLAN_DIRECTORY"),
    )
    backup_directory: Path = Field(
        default=Path("backups"),
        validation_alias=AliasChoices("BLD_BACKUP_DIRECTORY", "BGV_BACKUP_DIRECTORY"),
    )
    media_cache_directory: Path = Field(
        default=Path("data/media-cache"),
        validation_alias=AliasChoices(
            "BLD_MEDIA_CACHE_DIRECTORY", "BGV_MEDIA_CACHE_DIRECTORY"
        ),
    )
    image_policy: Literal["none", "metadata", "cache"] = Field(
        default="metadata",
        validation_alias=AliasChoices("BLD_IMAGE_POLICY", "BGV_IMAGE_POLICY"),
    )
    image_max_item_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1024,
        validation_alias=AliasChoices(
            "BLD_IMAGE_MAX_ITEM_BYTES", "BGV_IMAGE_MAX_ITEM_BYTES"
        ),
    )
    image_cache_max_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,
        ge=1024,
        validation_alias=AliasChoices(
            "BLD_IMAGE_CACHE_MAX_BYTES", "BGV_IMAGE_CACHE_MAX_BYTES"
        ),
    )
    steam_root: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("BLD_STEAM_ROOT", "STEAM_ROOT"),
    )
    steam_account_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BLD_STEAM_ACCOUNT_ID", "STEAM_ACCOUNT_ID"),
    )
    steam_config: Path = Field(
        default=Path("config/steam.toml"),
        validation_alias=AliasChoices("BLD_STEAM_CONFIG", "STEAM_CONFIG"),
    )
    steam_id64: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BLD_STEAM_ID64", "STEAM_ID64"),
    )
    steam_web_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("STEAM_WEB_API_KEY", "BLD_STEAM_WEB_API_KEY"),
    )
    bangumi_write_delay_ms: int = Field(default=500, ge=0)
    bangumi_max_retries: int = Field(default=4, ge=0, le=10)
    bangumi_retry_base_seconds: float = Field(default=0.5, gt=0)

    def require_bangumi(self) -> tuple[SecretStr, str, str]:
        missing: list[str] = []
        if self.bangumi_access_token is None or not self.bangumi_access_token.get_secret_value():
            missing.append("BANGUMI_ACCESS_TOKEN")
        if not self.bangumi_username or not self.bangumi_username.strip():
            missing.append("BANGUMI_USERNAME")
        if not self.bangumi_user_agent or not self.bangumi_user_agent.strip():
            missing.append("BANGUMI_USER_AGENT")
        if missing:
            raise ConfigurationError(f"Missing required setting(s): {', '.join(missing)}")

        assert self.bangumi_access_token is not None
        assert self.bangumi_username is not None
        assert self.bangumi_user_agent is not None
        return (
            self.bangumi_access_token,
            self.bangumi_username.strip(),
            self.bangumi_user_agent.strip(),
        )


def get_settings(env_file: str | Path | None = ".env") -> Settings:
    return Settings(_env_file=env_file)

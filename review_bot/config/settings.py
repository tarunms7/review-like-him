"""Pydantic settings model with environment variable support."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from review_bot.config.paths import CONFIG_DIR, DB_PATH


class Settings(BaseSettings):
    """Application settings loaded from environment variables and config."""

    model_config = SettingsConfigDict(
        env_prefix="REVIEW_BOT_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    github_app_id: int = Field(default=0, description="GitHub App ID")
    private_key_path: Path = Field(
        default=CONFIG_DIR / "private-key.pem",
        description="Path to GitHub App private key",
    )
    webhook_secret: str = Field(default="", description="GitHub webhook secret for HMAC validation")
    webhook_url: str = Field(default="", description="Public URL for receiving webhooks")
    db_url: str = Field(
        default=f"sqlite+aiosqlite:///{DB_PATH}",
        description="Database connection URL",
    )
    host: str = Field(default="0.0.0.0", description="Server bind host")
    port: int = Field(default=8000, description="Server bind port")

"""Pydantic settings model with environment variable support."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from review_bot.config.paths import CONFIG_DIR, DB_PATH

logger = logging.getLogger("review-bot")


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

    @field_validator("github_app_id")
    @classmethod
    def _validate_app_id(cls, v: int) -> int:
        """App ID must be positive when set (0 means unconfigured)."""
        if v < 0:
            raise ValueError("github_app_id must be >= 0")
        return v

    @field_validator("port")
    @classmethod
    def _validate_port(cls, v: int) -> int:
        """Port must be in valid range."""
        if not (1 <= v <= 65535):
            raise ValueError("port must be between 1 and 65535")
        return v

    def validate_for_server(self) -> list[str]:
        """Check all required server config is present.

        Returns:
            List of validation error messages. Empty list means valid.
        """
        errors: list[str] = []

        if self.github_app_id <= 0:
            errors.append(
                "github_app_id must be > 0. "
                "Set REVIEW_BOT_GITHUB_APP_ID or configure via 'review-bot init'."
            )

        if not self.webhook_secret:
            errors.append(
                "webhook_secret must be non-empty. "
                "Set REVIEW_BOT_WEBHOOK_SECRET or configure via 'review-bot init'."
            )

        # Validate private key path exists and is a file
        if not self.private_key_path.exists():
            errors.append(
                f"private_key_path does not exist: {self.private_key_path}. "
                f"Place your GitHub App private key there or set REVIEW_BOT_PRIVATE_KEY_PATH."
            )
        elif not self.private_key_path.is_file():
            errors.append(
                f"private_key_path is not a file: {self.private_key_path}"
            )
        else:
            # Check file permissions (should be 0600 for security)
            self._check_private_key_permissions(errors)

        # Check GITHUB_TOKEN scopes if available
        self._check_github_token_scopes()

        return errors

    def _check_private_key_permissions(self, errors: list[str]) -> None:
        """Warn if private key file has overly permissive permissions."""
        try:
            file_stat = self.private_key_path.stat()
            mode = stat.S_IMODE(file_stat.st_mode)
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                errors.append(
                    f"private_key_path has insecure permissions: {oct(mode)}. "
                    f"Run: chmod 600 {self.private_key_path}"
                )
        except OSError:
            pass  # Can't stat file, already caught by existence check

    @staticmethod
    def _check_github_token_scopes() -> None:
        """Log a warning if GITHUB_TOKEN is set but may lack required scopes."""
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            return

        # Fine-grained tokens start with "github_pat_", classic with "ghp_"
        if token.startswith("github_pat_"):
            logger.debug("Using fine-grained personal access token")
        elif token.startswith("ghp_"):
            logger.debug("Using classic personal access token")
        elif token.startswith("ghs_"):
            logger.debug("Using GitHub App installation token")
        else:
            logger.warning(
                "GITHUB_TOKEN has unrecognized format — "
                "ensure it has 'repo' and 'pull_request' scopes"
            )

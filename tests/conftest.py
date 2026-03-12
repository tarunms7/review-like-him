"""Common test fixtures for review-bot."""

from pathlib import Path

import pytest

from review_bot.config.settings import Settings


@pytest.fixture()
def tmp_config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory with standard subdirectories."""
    config_dir = tmp_path / ".review-bot"
    config_dir.mkdir()
    (config_dir / "personas").mkdir()
    (config_dir / "repos").mkdir()
    return config_dir


@pytest.fixture()
def mock_settings(tmp_config_dir: Path) -> Settings:
    """Create a Settings instance pointing to temporary directories."""
    db_path = tmp_config_dir / "review-bot.db"
    return Settings(
        github_app_id=12345,
        private_key_path=tmp_config_dir / "private-key.pem",
        webhook_secret="test-secret",
        webhook_url="http://localhost:8000/webhook",
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )

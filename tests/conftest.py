"""Common test fixtures for review-bot."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from review_bot.config.settings import Settings
from review_bot.github.api import GitHubAPIClient, PullRequestFile
from review_bot.persona.profile import PersonaProfile, Priority, SeverityPattern
from review_bot.persona.store import PersonaStore
from review_bot.review.formatter import (
    CategorySection,
    InlineComment,
    ReviewResult,
)


# ---------------------------------------------------------------------------
# Filesystem fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory with standard subdirectories."""
    config_dir = tmp_path / ".review-bot"
    config_dir.mkdir()
    (config_dir / "personas").mkdir()
    (config_dir / "repos").mkdir()
    return config_dir


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Persona fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_persona() -> PersonaProfile:
    """A fully-populated sample persona for testing."""
    return PersonaProfile(
        name="alice",
        github_user="alice-gh",
        mined_from="42 comments across 5 repos",
        last_updated="2025-12-01",
        priorities=[
            Priority(
                category="error_handling",
                severity="critical",
                description="Always check error returns",
            ),
            Priority(
                category="naming",
                severity="moderate",
                description="Use descriptive names",
            ),
        ],
        pet_peeves=["Magic numbers", "Missing docstrings"],
        tone="Direct but friendly",
        severity_pattern=SeverityPattern(
            blocks_on=["Unhandled errors", "Security issues"],
            nits_on=["Style inconsistencies"],
            approves_when="All errors handled and tests present",
        ),
        overrides=["Always check for type hints"],
    )


@pytest.fixture()
def minimal_persona() -> PersonaProfile:
    """A minimal persona with only required fields."""
    return PersonaProfile(
        name="bob",
        github_user="bob-gh",
    )


@pytest.fixture()
def persona_store(tmp_config_dir: Path) -> PersonaStore:
    """PersonaStore backed by a temporary directory."""
    return PersonaStore(base_dir=tmp_config_dir / "personas")


# ---------------------------------------------------------------------------
# GitHub / PR data fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_pr_data() -> dict:
    """Sample PR payload as returned by the GitHub API."""
    return {
        "number": 42,
        "title": "Add user authentication",
        "body": "Implements JWT-based auth flow",
        "user": {"login": "dev-user"},
        "additions": 150,
        "deletions": 20,
        "changed_files": 5,
        "html_url": "https://github.com/owner/repo/pull/42",
    }


@pytest.fixture()
def sample_pr_files() -> list[PullRequestFile]:
    """Sample list of changed files in a PR."""
    return [
        PullRequestFile(
            filename="src/auth.py",
            status="added",
            additions=100,
            deletions=0,
            patch="@@ -0,0 +1,100 @@\n+import jwt\n+...",
        ),
        PullRequestFile(
            filename="tests/test_auth.py",
            status="added",
            additions=50,
            deletions=0,
            patch="@@ -0,0 +1,50 @@\n+import pytest\n+...",
        ),
        PullRequestFile(
            filename="README.md",
            status="modified",
            additions=5,
            deletions=2,
            patch="@@ -1,5 +1,8 @@\n # Project\n+## Auth",
        ),
    ]


@pytest.fixture()
def sample_diff() -> str:
    """Sample unified diff text."""
    return (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/auth.py\n"
        "@@ -0,0 +1,5 @@\n"
        "+import jwt\n"
        "+\n"
        "+def authenticate(token):\n"
        "+    return jwt.decode(token, 'secret', algorithms=['HS256'])\n"
    )


# ---------------------------------------------------------------------------
# Mock GitHub client
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_github_client(sample_pr_data, sample_pr_files, sample_diff) -> GitHubAPIClient:
    """A GitHubAPIClient with all async methods mocked."""
    client = MagicMock(spec=GitHubAPIClient)
    client.get_pull_request = AsyncMock(return_value=sample_pr_data)
    client.get_pull_request_files = AsyncMock(return_value=sample_pr_files)
    client.get_pull_request_diff = AsyncMock(return_value=sample_diff)
    client.post_review = AsyncMock(return_value={"id": 1})
    client.post_comment = AsyncMock(return_value={"id": 2})
    client.get_repo_contents = AsyncMock(return_value=[])
    return client


# ---------------------------------------------------------------------------
# Mock Claude reviewer
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_claude_output() -> str:
    """Sample raw LLM output as valid JSON."""
    return json.dumps({
        "verdict": "request_changes",
        "summary_sections": [
            {
                "emoji": "🔒",
                "title": "Security",
                "findings": ["Hard-coded secret in jwt.decode"],
            },
            {
                "emoji": "🧪",
                "title": "Testing",
                "findings": ["No tests for auth module"],
            },
        ],
        "inline_comments": [
            {
                "file": "src/auth.py",
                "line": 4,
                "body": "Don't hard-code secrets — use env vars.",
            },
        ],
    })


# ---------------------------------------------------------------------------
# ReviewResult fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_review_result() -> ReviewResult:
    """A pre-built ReviewResult for tests that don't need to go through formatting."""
    return ReviewResult(
        verdict="request_changes",
        summary_sections=[
            CategorySection(
                emoji="🔒",
                title="Security",
                findings=["Hard-coded secret in jwt.decode"],
            ),
        ],
        inline_comments=[
            InlineComment(file="src/auth.py", line=4, body="Use env vars for secrets."),
        ],
        persona_name="alice",
        pr_url="https://github.com/owner/repo/pull/42",
    )

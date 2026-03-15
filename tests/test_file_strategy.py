"""Tests for file-type classification and review strategy system."""

from __future__ import annotations

from review_bot.github.api import PullRequestFile
from review_bot.review.file_strategy import (
    STRATEGIES,
    FileType,
    classify_file,
    get_file_strategies,
    get_strategy,
)


class TestClassifyFile:
    """Tests for the classify_file function."""

    def test_classify_python_migration(self) -> None:
        """Django-style migration detected."""
        assert classify_file("app/migrations/0001_initial.py") == FileType.MIGRATION

    def test_classify_sql_migration(self) -> None:
        """Rails/Flyway-style SQL migration detected."""
        assert (
            classify_file("db/migrate/202603151200_add_users.sql")
            == FileType.MIGRATION
        )

    def test_classify_alembic_migration(self) -> None:
        """Alembic migration detected via directory."""
        assert classify_file("alembic/versions/abc123.py") == FileType.MIGRATION

    def test_classify_test_file(self) -> None:
        """Python test file detected."""
        assert classify_file("tests/test_api.py") == FileType.TEST

    def test_classify_spec_file(self) -> None:
        """JavaScript/TypeScript spec file detected."""
        assert classify_file("src/components/Button.spec.tsx") == FileType.TEST

    def test_classify_generated_file(self) -> None:
        """Minified bundle detected as generated."""
        assert classify_file("dist/bundle.min.js") == FileType.GENERATED

    def test_classify_api_route(self) -> None:
        """API route file detected."""
        assert classify_file("src/api/routes/users.py") == FileType.API_DEFINITION

    def test_classify_dockerfile(self) -> None:
        """Dockerfile detected as infrastructure."""
        assert classify_file("Dockerfile") == FileType.INFRASTRUCTURE

    def test_classify_config(self) -> None:
        """pyproject.toml detected as config."""
        assert classify_file("pyproject.toml") == FileType.CONFIG

    def test_classify_markdown(self) -> None:
        """Markdown doc detected as documentation."""
        assert classify_file("docs/README.md") == FileType.DOCUMENTATION

    def test_classify_business_logic(self) -> None:
        """Source file without special patterns is business logic."""
        assert classify_file("src/services/payment.py") == FileType.BUSINESS_LOGIC

    def test_classify_unknown_extension(self) -> None:
        """Unrecognized extension falls back to UNKNOWN."""
        assert classify_file("data/file.xyz") == FileType.UNKNOWN


class TestGetFileStrategies:
    """Tests for the get_file_strategies function."""

    def _make_file(self, filename: str) -> PullRequestFile:
        """Create a PullRequestFile with minimal fields."""
        return PullRequestFile(
            filename=filename,
            status="modified",
            additions=10,
            deletions=5,
        )

    def test_get_file_strategies_groups_correctly(self) -> None:
        """Mixed file list is grouped by type."""
        files = [
            self._make_file("src/services/payment.py"),
            self._make_file("tests/test_payment.py"),
            self._make_file("docs/README.md"),
        ]
        groups = get_file_strategies(files)

        assert FileType.BUSINESS_LOGIC in groups
        assert FileType.TEST in groups
        assert FileType.DOCUMENTATION in groups
        assert len(groups[FileType.BUSINESS_LOGIC]) == 1
        assert len(groups[FileType.TEST]) == 1
        assert len(groups[FileType.DOCUMENTATION]) == 1

    def test_get_file_strategies_excludes_generated(self) -> None:
        """Generated files do not appear in any group."""
        files = [
            self._make_file("dist/bundle.min.js"),
            self._make_file("package-lock.json"),
            self._make_file("src/app.py"),
        ]
        groups = get_file_strategies(files)

        assert FileType.GENERATED not in groups
        assert FileType.BUSINESS_LOGIC in groups
        assert len(groups) == 1


class TestStrategies:
    """Tests for the STRATEGIES dict and get_strategy function."""

    def test_migration_strategy_instructions(self) -> None:
        """Migration strategy includes DROP/TRUNCATE rules."""
        strategy = get_strategy(FileType.MIGRATION)
        assert strategy is not None
        assert "DROP TABLE" in strategy.prompt_instructions
        assert "TRUNCATE" in strategy.prompt_instructions
        assert strategy.severity_boost == 1

    def test_all_strategies_have_required_fields(self) -> None:
        """Every strategy has non-empty focus and instructions."""
        for file_type, strategy in STRATEGIES.items():
            assert strategy.file_type == file_type
            assert strategy.display_name
            assert len(strategy.review_focus) > 0
            assert strategy.prompt_instructions

    def test_documentation_severity_boost(self) -> None:
        """Documentation strategy has negative severity boost."""
        strategy = get_strategy(FileType.DOCUMENTATION)
        assert strategy is not None
        assert strategy.severity_boost == -1

    def test_business_logic_severity_boost_default(self) -> None:
        """Business logic strategy has zero severity boost."""
        strategy = get_strategy(FileType.BUSINESS_LOGIC)
        assert strategy is not None
        assert strategy.severity_boost == 0

    def test_get_strategy_unknown_returns_none(self) -> None:
        """Unknown file type has no strategy."""
        assert get_strategy(FileType.UNKNOWN) is None

    def test_get_strategy_generated_returns_none(self) -> None:
        """Generated file type has no strategy."""
        assert get_strategy(FileType.GENERATED) is None

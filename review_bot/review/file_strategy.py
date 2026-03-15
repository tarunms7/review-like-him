"""File-type classification and review strategy system."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from review_bot.github.api import PullRequestFile

logger = logging.getLogger("review-bot")


class FileType:
    """Constants for recognized file types."""

    MIGRATION = "migration"
    BUSINESS_LOGIC = "business_logic"
    TEST = "test"
    CONFIG = "config"
    DOCUMENTATION = "documentation"
    API_DEFINITION = "api_definition"
    BUILD = "build"
    GENERATED = "generated"
    INFRASTRUCTURE = "infrastructure"
    UNKNOWN = "unknown"


@dataclass
class FileTypeStrategy:
    """Review strategy instructions for a specific file type.

    Args:
        file_type: FileType constant value (e.g. 'migration', 'test').
        display_name: Human-readable name for display.
        review_focus: List of focus areas for this file type.
        prompt_instructions: LLM prompt text specific to this file type's review strategy.
        severity_boost: Severity modifier: +1 for critical files, -1 for docs, 0 default.
    """

    file_type: str
    display_name: str
    review_focus: list[str]
    prompt_instructions: str
    severity_boost: int = 0


# Strategy definitions
STRATEGIES: dict[str, FileTypeStrategy] = {
    FileType.MIGRATION: FileTypeStrategy(
        file_type=FileType.MIGRATION,
        display_name="Database Migration",
        review_focus=[
            "Reversibility and rollback safety",
            "Missing transactions/atomic blocks",
            "Destructive operations (DROP, DELETE, TRUNCATE)",
            "Index creation on large tables (potential locks)",
            "Data type changes that may lose data",
            "Missing NOT NULL defaults for new columns",
        ],
        prompt_instructions="""\
**⚠️ DATABASE MIGRATION FILE — Apply heightened scrutiny:**
- Flag any `DROP TABLE`, `DROP COLUMN`, or `TRUNCATE` as HIGH severity unless wrapped in a safety check.
- Flag `ALTER TABLE` on large tables without concurrent index creation as a potential lock risk.
- Flag missing `BEGIN`/`COMMIT` or transaction wrapper (Django `atomic`, Alembic `op.batch_alter_table`).
- Flag adding NOT NULL columns without a default value (will fail on existing rows).
- Flag destructive operations that are not reversible (missing `down` migration or rollback).
- Flag raw SQL that could cause data loss without confirmation.
- Check for idempotency — migration should be safe to re-run.
""",
        severity_boost=1,
    ),
    FileType.BUSINESS_LOGIC: FileTypeStrategy(
        file_type=FileType.BUSINESS_LOGIC,
        display_name="Business Logic",
        review_focus=[
            "Architectural correctness and layer violations",
            "Business rule accuracy",
            "Error handling completeness",
            "Edge cases in logic branches",
            "Data validation",
        ],
        prompt_instructions="""\
**Business logic file — Focus on correctness and architecture:**
- Check for layer violations (e.g., HTTP/framework concerns leaking into domain logic).
- Verify error handling covers edge cases (empty inputs, boundary conditions, concurrent access).
- Check that business rules are clearly expressed and match expected behavior.
- Look for missing validation on inputs that cross trust boundaries.
- Flag complex conditional logic that lacks comments explaining the business rule.
""",
    ),
    FileType.TEST: FileTypeStrategy(
        file_type=FileType.TEST,
        display_name="Test File",
        review_focus=[
            "Test coverage adequacy",
            "Assertion quality and specificity",
            "Test isolation (no shared mutable state)",
            "Edge case coverage",
            "Test naming clarity",
        ],
        prompt_instructions="""\
**Test file — Focus on coverage quality and assertion rigor:**
- Check that tests cover both happy path and error/edge cases.
- Flag assertions that are too broad (e.g., `assert result is not None` when the value should be checked).
- Flag tests that test implementation details rather than behavior.
- Check for proper test isolation — no shared mutable state between tests.
- Flag missing `async` markers on async test functions (pytest-asyncio).
- Check that mock assertions verify the right arguments, not just call count.
- Flag flaky patterns: sleep-based waits, time-dependent assertions, network calls without mocking.
""",
    ),
    FileType.CONFIG: FileTypeStrategy(
        file_type=FileType.CONFIG,
        display_name="Configuration",
        review_focus=[
            "Secrets or credentials not hardcoded",
            "Environment-specific values not committed",
            "Schema validity",
        ],
        prompt_instructions="""\
**Configuration file — Focus on security and correctness:**
- Flag any hardcoded secrets, API keys, passwords, or tokens.
- Check that environment-specific values use environment variables or config references.
- Verify schema/format is valid for the config type (YAML, JSON, TOML).
- Flag overly permissive settings (e.g., CORS allow-all, debug mode enabled).
""",
    ),
    FileType.API_DEFINITION: FileTypeStrategy(
        file_type=FileType.API_DEFINITION,
        display_name="API Definition",
        review_focus=[
            "Backward compatibility",
            "Input validation",
            "Response schema consistency",
            "Error response format",
        ],
        prompt_instructions="""\
**API definition file — Focus on contract stability:**
- Flag breaking changes to existing endpoints (removed fields, changed types).
- Check that new endpoints have proper input validation.
- Verify error responses follow the project's error format convention.
- Check for missing authentication/authorization on new endpoints.
- Flag endpoints without rate limiting considerations.
""",
    ),
    FileType.INFRASTRUCTURE: FileTypeStrategy(
        file_type=FileType.INFRASTRUCTURE,
        display_name="Infrastructure/DevOps",
        review_focus=[
            "Security best practices",
            "Resource limits",
            "Secrets management",
        ],
        prompt_instructions="""\
**Infrastructure file — Focus on security and operational safety:**
- Flag hardcoded secrets or credentials.
- Check for missing resource limits (memory, CPU) in container configs.
- Verify that sensitive data uses secret management (not plain env vars in committed files).
- Flag overly permissive IAM/RBAC permissions.
""",
    ),
    FileType.DOCUMENTATION: FileTypeStrategy(
        file_type=FileType.DOCUMENTATION,
        display_name="Documentation",
        review_focus=[
            "Accuracy relative to code changes",
            "Completeness",
        ],
        prompt_instructions="""\
**Documentation file — Light review:**
- Check that documentation accurately reflects the code changes in this PR.
- Flag outdated examples or references to removed functionality.
- No need to nitpick prose style unless it's misleading.
""",
        severity_boost=-1,
    ),
}


def classify_file(filename: str) -> str:
    """Classify a file into a FileType based on name and extension.

    Classification priority:
    1. Explicit patterns (migrations, tests)
    2. Extension-based (configs, docs)
    3. Directory-based heuristics
    4. Fallback to UNKNOWN

    Args:
        filename: The file path/name to classify.

    Returns:
        A FileType string constant.
    """
    name_lower = filename.lower()
    basename = filename.rsplit("/", 1)[-1].lower()

    # Migration detection
    if any(pattern in name_lower for pattern in (
        "/migrations/",
        "/migrate/",
        "/alembic/",
        "/db/migrate/",
        "/flyway/",
    )) or name_lower.startswith(("migrations/", "alembic/", "flyway/")):
        return FileType.MIGRATION
    if re.search(r"\d{3,}_\w+\.(py|sql|rb)$", basename):
        return FileType.MIGRATION  # Numbered migration files

    # Test detection
    if any(pattern in name_lower for pattern in (
        "test_", "_test.", ".test.", ".spec.", "/tests/", "/__tests__/",
        "/test/", "/spec/", "conftest.py",
    )):
        return FileType.TEST

    # Generated file detection
    if any(pattern in name_lower for pattern in (
        ".min.js", ".min.css", ".generated.", "/generated/",
        "package-lock.json", "yarn.lock", "poetry.lock",
        "Pipfile.lock", "/dist/", "/build/",
    )):
        return FileType.GENERATED

    # API definition detection
    if any(pattern in name_lower for pattern in (
        "openapi", "swagger", ".proto", ".graphql", ".gql",
        "/routes/", "/endpoints/", "/handlers/",
        "/api/", "router", "controller",
    )):
        if not name_lower.endswith((".md", ".txt")):
            return FileType.API_DEFINITION

    # Infrastructure detection
    if any(pattern in name_lower for pattern in (
        "dockerfile", "docker-compose", ".tf", ".hcl",
        "kubernetes", "k8s", "helm", ".github/workflows",
        "jenkinsfile", ".circleci", "ansible", "terraform",
    )):
        return FileType.INFRASTRUCTURE

    # Config detection
    config_extensions = {
        ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
        ".env", ".env.example", ".json",
    }
    config_names = {
        "pyproject.toml", "setup.cfg", "setup.py", "tsconfig.json",
        "webpack.config.js", "vite.config.ts", "eslint.config.js",
        ".eslintrc", ".prettierrc", "biome.json",
    }
    ext = "." + basename.rsplit(".", 1)[-1] if "." in basename else ""
    if basename in config_names or (ext in config_extensions and "/" not in filename):
        return FileType.CONFIG

    # Documentation detection
    doc_extensions = {".md", ".rst", ".txt", ".adoc"}
    if ext in doc_extensions:
        return FileType.DOCUMENTATION

    # Business logic — source code that doesn't match other categories
    source_extensions = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
        ".java", ".kt", ".rb", ".ex", ".exs", ".php",
        ".cs", ".swift", ".scala", ".clj",
    }
    if ext in source_extensions:
        return FileType.BUSINESS_LOGIC

    return FileType.UNKNOWN


def get_strategy(file_type: str) -> FileTypeStrategy | None:
    """Get the review strategy for a file type.

    Args:
        file_type: A FileType string constant.

    Returns:
        The FileTypeStrategy for the given type, or None if not found.
    """
    return STRATEGIES.get(file_type)


def get_file_strategies(
    files: list[PullRequestFile],
) -> dict[str, list[PullRequestFile]]:
    """Group PR files by their file type classification.

    Generated files are excluded entirely from the result.

    Args:
        files: List of PullRequestFile objects from a PR.

    Returns:
        A dict of FileType string → list of PullRequestFile.
    """
    groups: dict[str, list[PullRequestFile]] = {}
    for f in files:
        ft = classify_file(f.filename)
        if ft == FileType.GENERATED:
            continue  # Skip generated files entirely
        groups.setdefault(ft, []).append(f)
    return groups

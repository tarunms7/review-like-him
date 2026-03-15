"""Tests for the RepoScanner context-aware repository scanning."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import httpx
import pytest

from review_bot.review.repo_scanner import (
    APIContract,
    ModuleBoundary,
    OwnershipHint,
    RepoContext,
    RepoScanner,
    _MAX_CONTEXT_SIZE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64(text: str) -> str:
    """Encode text as base64 for mock file content responses."""
    return base64.b64encode(text.encode()).decode()


def _dir_entry(name: str, entry_type: str = "file") -> dict:
    """Create a mock directory entry."""
    return {"name": name, "type": entry_type}


# ---------------------------------------------------------------------------
# Module detection tests
# ---------------------------------------------------------------------------

class TestDetectModules:
    """Tests for _detect_modules method."""

    @pytest.mark.asyncio()
    async def test_detect_modules_python_packages(
        self, mock_github_client,
    ) -> None:
        """Detect Python packages by __init__.py presence."""
        root_contents = [
            _dir_entry("review_bot", "dir"),
            _dir_entry("tests", "dir"),
            _dir_entry("pyproject.toml"),
        ]

        async def mock_get_contents(owner, repo, path):
            if path == "":
                return root_contents
            if path == "review_bot":
                return [
                    _dir_entry("__init__.py"),
                    _dir_entry("cli", "dir"),
                    _dir_entry("review", "dir"),
                ]
            if path == "review_bot/cli":
                return [_dir_entry("__init__.py"), _dir_entry("main.py")]
            if path == "review_bot/review":
                return [_dir_entry("__init__.py"), _dir_entry("app.py")]
            if path == "tests":
                return [_dir_entry("__init__.py"), _dir_entry("conftest.py")]
            return []

        mock_github_client.get_repo_contents = AsyncMock(
            side_effect=mock_get_contents,
        )

        scanner = RepoScanner(mock_github_client)
        modules = await scanner._detect_modules("owner", "repo", root_contents)

        paths = {m.path for m in modules}
        assert "review_bot" in paths
        assert "review_bot/cli" in paths
        assert "review_bot/review" in paths
        assert "tests" in paths

        # Check purpose inference
        cli_mod = next(m for m in modules if m.path == "review_bot/cli")
        assert cli_mod.purpose == "cli"
        assert "__init__.py" in cli_mod.entry_points
        assert "main.py" in cli_mod.entry_points

        tests_mod = next(m for m in modules if m.path == "tests")
        assert tests_mod.purpose == "tests"

    @pytest.mark.asyncio()
    async def test_detect_modules_javascript(
        self, mock_github_client,
    ) -> None:
        """Detect JavaScript modules by index.js/index.ts presence."""
        root_contents = [
            _dir_entry("src", "dir"),
            _dir_entry("package.json"),
        ]

        async def mock_get_contents(owner, repo, path):
            if path == "":
                return root_contents
            if path == "src":
                return [
                    _dir_entry("index.ts"),
                    _dir_entry("api", "dir"),
                    _dir_entry("utils", "dir"),
                ]
            if path == "src/api":
                return [_dir_entry("index.ts")]
            if path == "src/utils":
                return [_dir_entry("index.js")]
            return []

        mock_github_client.get_repo_contents = AsyncMock(
            side_effect=mock_get_contents,
        )

        scanner = RepoScanner(mock_github_client)
        modules = await scanner._detect_modules("owner", "repo", root_contents)

        paths = {m.path for m in modules}
        assert "src" in paths
        assert "src/api" in paths
        assert "src/utils" in paths

        api_mod = next(m for m in modules if m.path == "src/api")
        assert api_mod.purpose == "api"
        assert "index.ts" in api_mod.entry_points

        utils_mod = next(m for m in modules if m.path == "src/utils")
        assert utils_mod.purpose == "utils"


# ---------------------------------------------------------------------------
# API contract detection tests
# ---------------------------------------------------------------------------

class TestDetectAPIContracts:
    """Tests for _detect_api_contracts method."""

    @pytest.mark.asyncio()
    async def test_detect_api_contracts_fastapi(
        self, mock_github_client,
    ) -> None:
        """Detect FastAPI route decorators."""
        modules = [
            ModuleBoundary(
                path="src/api",
                purpose="api",
                entry_points=["app.py"],
            ),
        ]

        fastapi_content = (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            "@app.get('/users')\n"
            "async def list_users():\n"
            "    return []\n"
        )

        async def mock_get_contents(owner, repo, path):
            if path == "src/api/app.py":
                return {"content": _b64(fastapi_content)}
            if path == "src/api":
                return [_dir_entry("app.py"), _dir_entry("models.py")]
            return []

        mock_github_client.get_repo_contents = AsyncMock(
            side_effect=mock_get_contents,
        )

        scanner = RepoScanner(mock_github_client)
        contracts = await scanner._detect_api_contracts(
            "owner", "repo", modules,
        )

        assert len(contracts) >= 1
        fastapi_contracts = [
            c for c in contracts if c.contract_type == "fastapi_route"
        ]
        assert len(fastapi_contracts) == 1
        assert fastapi_contracts[0].file == "src/api/app.py"

    @pytest.mark.asyncio()
    async def test_detect_api_contracts_graphql_and_proto(
        self, mock_github_client,
    ) -> None:
        """Detect GraphQL and protobuf files in module directories."""
        modules = [
            ModuleBoundary(
                path="src/api",
                purpose="api",
                entry_points=["index.ts"],
            ),
        ]

        async def mock_get_contents(owner, repo, path):
            if path == "src/api/index.ts":
                return {"content": _b64("export default {}")}
            if path == "src/api":
                return [
                    _dir_entry("index.ts"),
                    _dir_entry("schema.graphql"),
                    _dir_entry("service.proto"),
                    _dir_entry("openapi.yaml"),
                ]
            return []

        mock_github_client.get_repo_contents = AsyncMock(
            side_effect=mock_get_contents,
        )

        scanner = RepoScanner(mock_github_client)
        contracts = await scanner._detect_api_contracts(
            "owner", "repo", modules,
        )

        contract_types = {c.contract_type for c in contracts}
        assert "graphql_schema" in contract_types
        assert "protobuf" in contract_types
        assert "openapi_spec" in contract_types


# ---------------------------------------------------------------------------
# Ownership detection tests
# ---------------------------------------------------------------------------

class TestDetectOwnership:
    """Tests for _detect_ownership method."""

    @pytest.mark.asyncio()
    async def test_detect_ownership_codeowners(
        self, mock_github_client,
    ) -> None:
        """Parse CODEOWNERS file into ownership hints."""
        root_contents = [
            _dir_entry("CODEOWNERS"),
            _dir_entry("src", "dir"),
        ]

        codeowners = (
            "# Global owners\n"
            "* @global-owner\n"
            "*.py @python-team\n"
            "src/api/ @api-team @backend-lead\n"
            "\n"
            "# Docs\n"
            "docs/ @docs-team\n"
        )

        async def mock_get_contents(owner, repo, path):
            if path == "CODEOWNERS":
                return {"content": _b64(codeowners)}
            return root_contents

        mock_github_client.get_repo_contents = AsyncMock(
            side_effect=mock_get_contents,
        )

        scanner = RepoScanner(mock_github_client)
        ownership = await scanner._detect_ownership(
            "owner", "repo", root_contents,
        )

        assert len(ownership) == 4
        assert ownership[0].pattern == "*"
        assert ownership[0].owners == ["@global-owner"]
        assert ownership[2].pattern == "src/api/"
        assert "@api-team" in ownership[2].owners
        assert "@backend-lead" in ownership[2].owners

    @pytest.mark.asyncio()
    async def test_detect_ownership_no_codeowners(
        self, mock_github_client,
    ) -> None:
        """Return empty list when no CODEOWNERS file exists."""
        root_contents = [_dir_entry("src", "dir")]

        mock_github_client.get_repo_contents = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Not Found",
                request=httpx.Request("GET", "https://api.github.com"),
                response=httpx.Response(404),
            ),
        )

        scanner = RepoScanner(mock_github_client)
        ownership = await scanner._detect_ownership(
            "owner", "repo", root_contents,
        )

        assert ownership == []


# ---------------------------------------------------------------------------
# README architecture extraction tests
# ---------------------------------------------------------------------------

class TestParseReadmeArchitecture:
    """Tests for _parse_readme_architecture method."""

    @pytest.mark.asyncio()
    async def test_parse_readme_architecture(
        self, mock_github_client,
    ) -> None:
        """Extract architecture sections from README."""
        readme = (
            "# My Project\n\n"
            "Some intro text.\n\n"
            "## Architecture\n\n"
            "This project uses a layered architecture:\n"
            "- API layer handles HTTP\n"
            "- Service layer has business logic\n"
            "- Data layer manages persistence\n\n"
            "## Installation\n\n"
            "Run pip install.\n"
        )

        async def mock_get_contents(owner, repo, path):
            if path == "README.md":
                return {"content": _b64(readme)}
            return []

        mock_github_client.get_repo_contents = AsyncMock(
            side_effect=mock_get_contents,
        )

        scanner = RepoScanner(mock_github_client)
        notes = await scanner._parse_readme_architecture("owner", "repo")

        assert len(notes) == 1
        assert "layered architecture" in notes[0]
        assert "API layer" in notes[0]

    @pytest.mark.asyncio()
    async def test_parse_readme_no_architecture_section(
        self, mock_github_client,
    ) -> None:
        """Return empty list when README has no architecture section."""
        readme = "# Project\n\nJust a simple project.\n"

        mock_github_client.get_repo_contents = AsyncMock(
            return_value={"content": _b64(readme)},
        )

        scanner = RepoScanner(mock_github_client)
        notes = await scanner._parse_readme_architecture("owner", "repo")

        assert notes == []


# ---------------------------------------------------------------------------
# Project type detection tests
# ---------------------------------------------------------------------------

class TestDetectProjectType:
    """Tests for _detect_project_type static method."""

    def test_detect_project_type_monorepo(self) -> None:
        """Detect monorepo from lerna.json or packages/ dir."""
        root_contents = [
            _dir_entry("lerna.json"),
            _dir_entry("packages", "dir"),
            _dir_entry("package.json"),
        ]

        result = RepoScanner._detect_project_type(root_contents, [])
        assert result == "monorepo"

    def test_detect_project_type_monorepo_packages_dir(self) -> None:
        """Detect monorepo from packages/ directory alone."""
        root_contents = [
            _dir_entry("packages", "dir"),
            _dir_entry("package.json"),
        ]

        result = RepoScanner._detect_project_type(root_contents, [])
        assert result == "monorepo"

    def test_detect_project_type_library(self) -> None:
        """Detect library from setup.py + src/ layout."""
        root_contents = [
            _dir_entry("setup.py"),
            _dir_entry("src", "dir"),
            _dir_entry("pyproject.toml"),
        ]

        result = RepoScanner._detect_project_type(root_contents, [])
        assert result == "library"

    def test_detect_project_type_microservice(self) -> None:
        """Detect microservice from Dockerfile + small module count."""
        root_contents = [
            _dir_entry("Dockerfile"),
            _dir_entry("src", "dir"),
        ]
        modules = [
            ModuleBoundary(path="src", purpose="unknown", entry_points=[]),
        ]

        result = RepoScanner._detect_project_type(root_contents, modules)
        assert result == "microservice"

    def test_detect_project_type_application(self) -> None:
        """Detect application from manage.py."""
        root_contents = [
            _dir_entry("manage.py"),
            _dir_entry("myapp", "dir"),
        ]

        result = RepoScanner._detect_project_type(root_contents, [])
        assert result == "application"

    def test_detect_project_type_unknown(self) -> None:
        """Return 'unknown' when no heuristics match."""
        root_contents = [_dir_entry("README.md")]

        result = RepoScanner._detect_project_type(root_contents, [])
        assert result == "unknown"


# ---------------------------------------------------------------------------
# Import graph analysis tests
# ---------------------------------------------------------------------------

class TestAnalyzeImportGraph:
    """Tests for _analyze_import_graph method."""

    @pytest.mark.asyncio()
    async def test_import_graph_detects_cycles(
        self, mock_github_client,
    ) -> None:
        """Detect circular dependencies in import graph."""
        modules = [
            ModuleBoundary(
                path="review_bot/api",
                purpose="api",
                entry_points=["__init__.py"],
            ),
            ModuleBoundary(
                path="review_bot/services",
                purpose="services",
                entry_points=["__init__.py"],
            ),
        ]

        api_content = (
            "from review_bot.services import UserService\n"
        )
        services_content = (
            "from review_bot.api import router\n"
        )

        async def mock_get_contents(owner, repo, path):
            if path == "review_bot/api/__init__.py":
                return {"content": _b64(api_content)}
            if path == "review_bot/services/__init__.py":
                return {"content": _b64(services_content)}
            return []

        mock_github_client.get_repo_contents = AsyncMock(
            side_effect=mock_get_contents,
        )

        scanner = RepoScanner(mock_github_client)
        summary = await scanner._analyze_import_graph(
            "owner", "repo", modules,
        )

        assert "Circular dependencies" in summary
        assert "2 modules" in summary

    @pytest.mark.asyncio()
    async def test_import_graph_no_modules(
        self, mock_github_client,
    ) -> None:
        """Return empty string when no modules exist."""
        scanner = RepoScanner(mock_github_client)
        summary = await scanner._analyze_import_graph("owner", "repo", [])
        assert summary == ""


# ---------------------------------------------------------------------------
# Context size capping tests
# ---------------------------------------------------------------------------

class TestContextSizeCapped:
    """Tests for context size limiting."""

    @pytest.mark.asyncio()
    async def test_context_size_capped(
        self, mock_github_client,
    ) -> None:
        """Architecture notes should be capped at _MAX_CONTEXT_SIZE."""
        # Create a README with a very large architecture section
        large_section = "x" * (_MAX_CONTEXT_SIZE + 1000)
        readme = (
            "# Project\n\n"
            "## Architecture\n\n"
            f"{large_section}\n\n"
            "## Other\n"
        )

        async def mock_get_contents(owner, repo, path):
            if path == "README.md":
                return {"content": _b64(readme)}
            return []

        mock_github_client.get_repo_contents = AsyncMock(
            side_effect=mock_get_contents,
        )

        scanner = RepoScanner(mock_github_client)
        notes = await scanner._parse_readme_architecture("owner", "repo")

        total_len = sum(len(n) for n in notes)
        assert total_len <= _MAX_CONTEXT_SIZE


# ---------------------------------------------------------------------------
# Repo config reader tests
# ---------------------------------------------------------------------------

class TestReadRepoConfig:
    """Tests for _read_repo_config method."""

    @pytest.mark.asyncio()
    async def test_read_repo_config_valid_yaml(
        self, mock_github_client,
    ) -> None:
        """Successfully read and parse .review-like-him.yml."""
        yaml_content = (
            "severity:\n"
            "  min_level: warning\n"
            "  block_on:\n"
            "    - security\n"
            "    - error_handling\n"
        )

        mock_github_client.get_repo_contents = AsyncMock(
            return_value={"content": _b64(yaml_content)},
        )

        scanner = RepoScanner(mock_github_client)
        config = await scanner._read_repo_config("owner", "repo")

        assert config is not None
        assert "severity" in config
        assert config["severity"]["min_level"] == "warning"
        assert "security" in config["severity"]["block_on"]

    @pytest.mark.asyncio()
    async def test_read_repo_config_missing(
        self, mock_github_client,
    ) -> None:
        """Return None when .review-like-him.yml doesn't exist."""
        mock_github_client.get_repo_contents = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Not Found",
                request=httpx.Request("GET", "https://api.github.com"),
                response=httpx.Response(404),
            ),
        )

        scanner = RepoScanner(mock_github_client)
        config = await scanner._read_repo_config("owner", "repo")

        assert config is None

    @pytest.mark.asyncio()
    async def test_read_repo_config_invalid_yaml(
        self, mock_github_client,
    ) -> None:
        """Return None for invalid YAML content."""
        invalid_yaml = ": : : not valid yaml [["

        mock_github_client.get_repo_contents = AsyncMock(
            return_value={"content": _b64(invalid_yaml)},
        )

        scanner = RepoScanner(mock_github_client)
        config = await scanner._read_repo_config("owner", "repo")

        assert config is None


# ---------------------------------------------------------------------------
# RepoContext backward compatibility tests
# ---------------------------------------------------------------------------

class TestRepoContextBackwardCompat:
    """Ensure new fields have defaults for backward compatibility."""

    def test_new_fields_have_defaults(self) -> None:
        """RepoContext can be created with no arguments."""
        ctx = RepoContext()
        assert ctx.modules == []
        assert ctx.api_contracts == []
        assert ctx.ownership == []
        assert ctx.architecture_notes == []
        assert ctx.project_type == "unknown"
        assert ctx.import_graph_summary == ""

    def test_original_fields_preserved(self) -> None:
        """Original fields still work as before."""
        ctx = RepoContext(
            languages=["python"],
            frameworks=["fastapi"],
            has_tests=True,
            test_frameworks=["pytest"],
            has_ci=True,
            ci_systems=["github_actions"],
            has_linting=True,
            linters=["ruff"],
        )
        assert ctx.languages == ["python"]
        assert ctx.has_ci is True


# ---------------------------------------------------------------------------
# Full scan integration test
# ---------------------------------------------------------------------------

class TestScanIntegration:
    """Integration test for the full scan() method."""

    @pytest.mark.asyncio()
    async def test_scan_populates_new_fields(
        self, mock_github_client,
    ) -> None:
        """Verify scan() populates modules, ownership, and project_type."""
        root_contents = [
            _dir_entry("pyproject.toml"),
            _dir_entry("src", "dir"),
            _dir_entry("CODEOWNERS"),
            _dir_entry("README.md"),
            _dir_entry("Dockerfile"),
        ]

        codeowners = "*.py @python-team\n"
        readme = "# Project\n\n## Architecture\n\nLayered design.\n"

        async def mock_get_contents(owner, repo, path):
            if path == "":
                return root_contents
            if path == "src":
                return [_dir_entry("__init__.py"), _dir_entry("main.py")]
            if path == "pyproject.toml":
                return {"content": _b64("[project]\nname = 'myapp'\n")}
            if path == "CODEOWNERS":
                return {"content": _b64(codeowners)}
            if path == "README.md":
                return {"content": _b64(readme)}
            if path == ".review-like-him.yml":
                raise httpx.HTTPStatusError(
                    "Not Found",
                    request=httpx.Request("GET", "https://api.github.com"),
                    response=httpx.Response(404),
                )
            return []

        mock_github_client.get_repo_contents = AsyncMock(
            side_effect=mock_get_contents,
        )

        scanner = RepoScanner(mock_github_client)
        ctx = await scanner.scan("owner", "repo")

        # Check new fields are populated
        assert len(ctx.modules) >= 1
        assert any(m.path == "src" for m in ctx.modules)
        assert len(ctx.ownership) == 1
        assert ctx.ownership[0].pattern == "*.py"
        assert len(ctx.architecture_notes) >= 1
        assert "Layered design" in ctx.architecture_notes[0]
        assert ctx.project_type == "microservice"  # Dockerfile + small modules

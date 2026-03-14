"""Repo convention scanner: auto-detect languages, frameworks, CI, and linting."""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, Field

from review_bot.github.api import GitHubAPIClient

logger = logging.getLogger("review-bot")

# Mapping of marker files to detected attributes
_LANGUAGE_MARKERS: dict[str, str] = {
    "requirements.txt": "python",
    "setup.py": "python",
    "pyproject.toml": "python",
    "Pipfile": "python",
    "package.json": "javascript",
    "tsconfig.json": "typescript",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "java",
    "build.gradle": "java",
    "Gemfile": "ruby",
    "mix.exs": "elixir",
    "composer.json": "php",
}

_CONFIG_MARKERS: dict[str, str] = {
    "setup.py": "python",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "java",
}

_FRAMEWORK_MARKERS: dict[str, str] = {
    "next.config.js": "next",
    "next.config.mjs": "next",
    "nuxt.config.js": "nuxt",
    "angular.json": "angular",
    "vite.config.ts": "vite",
    "vite.config.js": "vite",
}

_TEST_DIR_MARKERS: list[str] = [
    "tests",
    "test",
    "spec",
    "__tests__",
]

_TEST_FRAMEWORK_FILES: dict[str, str] = {
    "pytest.ini": "pytest",
    "conftest.py": "pytest",
    "setup.cfg": "pytest",
    "jest.config.js": "jest",
    "jest.config.ts": "jest",
    "vitest.config.ts": "vitest",
    ".mocharc.yml": "mocha",
    "karma.conf.js": "karma",
}

_CI_MARKERS: dict[str, str] = {
    ".github/workflows": "github_actions",
    ".circleci": "circleci",
    ".gitlab-ci.yml": "gitlab_ci",
    "Jenkinsfile": "jenkins",
    ".travis.yml": "travis",
    "azure-pipelines.yml": "azure_devops",
    "bitbucket-pipelines.yml": "bitbucket",
}

_LINTER_MARKERS: dict[str, str] = {
    ".eslintrc": "eslint",
    ".eslintrc.js": "eslint",
    ".eslintrc.json": "eslint",
    ".eslintrc.yml": "eslint",
    "eslint.config.js": "eslint",
    "eslint.config.mjs": "eslint",
    ".prettierrc": "prettier",
    ".prettierrc.json": "prettier",
    "prettier.config.js": "prettier",
    "ruff.toml": "ruff",
    ".flake8": "flake8",
    "mypy.ini": "mypy",
    ".pylintrc": "pylint",
    "biome.json": "biome",
    "stylelint.config.js": "stylelint",
    ".stylelintrc": "stylelint",
}


class RepoContext(BaseModel):
    """Auto-detected repository conventions and context."""

    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    has_tests: bool = False
    test_frameworks: list[str] = Field(default_factory=list)
    has_ci: bool = False
    ci_systems: list[str] = Field(default_factory=list)
    has_linting: bool = False
    linters: list[str] = Field(default_factory=list)


class RepoScanner:
    """Scans a GitHub repository to detect conventions."""

    def __init__(self, github_client: GitHubAPIClient) -> None:
        self._client = github_client

    async def scan(self, owner: str, repo: str) -> RepoContext:
        """Scan a repository and return detected conventions."""
        root_contents = await self._list_dir(owner, repo, "")
        if root_contents is None:
            logger.warning("Could not read repo root for %s/%s", owner, repo)
            return RepoContext()

        root_names = {item["name"] for item in root_contents}
        root_dirs = {item["name"] for item in root_contents if item.get("type") == "dir"}

        languages = self._detect_languages(root_names)
        frameworks = self._detect_frameworks(root_names)

        # Check pyproject.toml for ruff and framework hints
        if "pyproject.toml" in root_names:
            pyproject = await self._read_file(owner, repo, "pyproject.toml")
            if pyproject:
                frameworks, languages = self._parse_pyproject(
                    pyproject,
                    frameworks,
                    languages,
                )

        # Check package.json for framework hints
        if "package.json" in root_names:
            pkg = await self._read_file(owner, repo, "package.json")
            if pkg:
                frameworks = self._parse_package_json(pkg, frameworks)

        test_frameworks = self._detect_test_frameworks(root_names)
        has_tests = bool(test_frameworks) or bool(root_dirs & set(_TEST_DIR_MARKERS))

        ci_systems = await self._detect_ci(owner, repo, root_names, root_dirs)
        linters = self._detect_linters(root_names)

        # Check pyproject.toml for ruff config
        if "pyproject.toml" in root_names and "ruff" not in linters:
            pyproject = await self._read_file(owner, repo, "pyproject.toml")
            if pyproject and "[tool.ruff]" in pyproject:
                linters.append("ruff")

        return RepoContext(
            languages=sorted(set(languages)),
            frameworks=sorted(set(frameworks)),
            has_tests=has_tests,
            test_frameworks=sorted(set(test_frameworks)),
            has_ci=bool(ci_systems),
            ci_systems=sorted(set(ci_systems)),
            has_linting=bool(linters),
            linters=sorted(set(linters)),
        )

    async def _list_dir(
        self,
        owner: str,
        repo: str,
        path: str,
    ) -> list[dict] | None:
        """List directory contents, returning None on failure."""
        try:
            result = await self._client.get_repo_contents(owner, repo, path)
            if isinstance(result, list):
                return result
            return None
        except httpx.HTTPStatusError:
            return None

    async def _read_file(
        self,
        owner: str,
        repo: str,
        path: str,
    ) -> str | None:
        """Read a file's text content, returning None on failure."""
        try:
            import base64

            result = await self._client.get_repo_contents(owner, repo, path)
            if isinstance(result, dict) and result.get("content"):
                return base64.b64decode(result["content"]).decode("utf-8")
            return None
        except httpx.HTTPStatusError:
            logger.warning("HTTP error reading file %s in %s/%s", path, owner, repo)
            return None
        except Exception:
            logger.warning(
                "Unexpected error reading file %s in %s/%s",
                path, owner, repo, exc_info=True,
            )
            return None

    def _detect_languages(self, root_names: set[str]) -> list[str]:
        """Detect programming languages from marker files."""
        langs: list[str] = []
        for marker, lang in _LANGUAGE_MARKERS.items():
            if marker in root_names and lang not in langs:
                langs.append(lang)
        return langs

    def _detect_frameworks(self, root_names: set[str]) -> list[str]:
        """Detect frameworks from marker files."""
        frameworks: list[str] = []
        for marker, fw in _FRAMEWORK_MARKERS.items():
            if marker in root_names and fw not in frameworks:
                frameworks.append(fw)
        return frameworks

    def _detect_test_frameworks(self, root_names: set[str]) -> list[str]:
        """Detect test frameworks from config files."""
        frameworks: list[str] = []
        for marker, fw in _TEST_FRAMEWORK_FILES.items():
            if marker in root_names and fw not in frameworks:
                frameworks.append(fw)
        return frameworks

    async def _detect_ci(
        self,
        owner: str,
        repo: str,
        root_names: set[str],
        root_dirs: set[str],
    ) -> list[str]:
        """Detect CI systems from config files and directories."""
        systems: list[str] = []
        for marker, ci in _CI_MARKERS.items():
            if "/" in marker:
                # Directory-based marker — validate it has actual workflow files
                dir_name = marker.split("/")[0]
                subpath = marker  # e.g. ".github/workflows"
                if dir_name in root_dirs:
                    contents = await self._list_dir(owner, repo, subpath)
                    if contents and any(
                        item.get("name", "").endswith((".yml", ".yaml"))
                        for item in contents
                    ):
                        systems.append(ci)
                    else:
                        logger.debug(
                            "CI directory %s exists but has no workflow files",
                            subpath,
                        )
            elif marker in root_names:
                systems.append(ci)
        return systems

    def _detect_linters(self, root_names: set[str]) -> list[str]:
        """Detect linters/formatters from config files."""
        linters: list[str] = []
        for marker, linter in _LINTER_MARKERS.items():
            if marker in root_names and linter not in linters:
                linters.append(linter)
        return linters

    def _parse_pyproject(
        self,
        content: str,
        frameworks: list[str],
        languages: list[str],
    ) -> tuple[list[str], list[str]]:
        """Extract framework/language hints from pyproject.toml."""
        frameworks = list(frameworks)
        languages = list(languages)

        if "fastapi" in content.lower():
            if "fastapi" not in frameworks:
                frameworks.append("fastapi")
        if "django" in content.lower():
            if "django" not in frameworks:
                frameworks.append("django")
        if "flask" in content.lower():
            if "flask" not in frameworks:
                frameworks.append("flask")

        return frameworks, languages

    def _parse_package_json(
        self,
        content: str,
        frameworks: list[str],
    ) -> list[str]:
        """Extract framework hints from package.json."""
        import json

        frameworks = list(frameworks)
        try:
            pkg = json.loads(content)
        except json.JSONDecodeError:
            return frameworks

        all_deps = {}
        all_deps.update(pkg.get("dependencies", {}))
        all_deps.update(pkg.get("devDependencies", {}))

        dep_framework_map = {
            "react": "react",
            "vue": "vue",
            "svelte": "svelte",
            "@angular/core": "angular",
            "express": "express",
            "fastify": "fastify",
        }
        for dep, fw in dep_framework_map.items():
            if dep in all_deps and fw not in frameworks:
                frameworks.append(fw)

        return frameworks

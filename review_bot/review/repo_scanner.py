"""Repo convention scanner: auto-detect languages, frameworks, CI, and linting."""

from __future__ import annotations

import logging
import re

import httpx
import yaml
from pydantic import BaseModel, Field, ValidationError

from review_bot.config.repo_config import RepoConfig
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

# Purpose inference from directory names
_PURPOSE_MAP: dict[str, str] = {
    "api": "api",
    "routes": "api",
    "endpoints": "api",
    "handlers": "api",
    "views": "api",
    "models": "models",
    "schemas": "models",
    "entities": "models",
    "tests": "tests",
    "test": "tests",
    "spec": "tests",
    "__tests__": "tests",
    "utils": "utils",
    "helpers": "utils",
    "lib": "utils",
    "common": "utils",
    "shared": "utils",
    "config": "config",
    "settings": "config",
    "db": "database",
    "database": "database",
    "migrations": "database",
    "middleware": "middleware",
    "services": "services",
    "cli": "cli",
    "commands": "cli",
    "static": "static",
    "public": "static",
    "assets": "static",
    "templates": "templates",
    "docs": "docs",
}

# Entry point filenames to look for in modules
_ENTRY_POINT_FILES: set[str] = {
    "__init__.py",
    "index.ts",
    "index.js",
    "index.tsx",
    "index.jsx",
    "mod.rs",
    "main.py",
    "main.ts",
    "main.js",
    "main.go",
    "app.py",
    "app.ts",
    "app.js",
}

# API contract detection patterns
_FASTAPI_PATTERN: re.Pattern[str] = re.compile(
    r"@(?:app|router)\.(get|post|put|patch|delete)\s*\(",
    re.IGNORECASE,
)
_FLASK_PATTERN: re.Pattern[str] = re.compile(
    r"@(?:app|blueprint|bp)\.(route|get|post|put|patch|delete)\s*\(",
    re.IGNORECASE,
)

# Architecture section header patterns in README
_ARCHITECTURE_HEADERS: re.Pattern[str] = re.compile(
    r"^#{1,3}\s+(Architecture|Structure|Design|System\s+Design|"
    r"Project\s+Structure|Technical\s+Overview|High.?Level\s+Design)",
    re.IGNORECASE | re.MULTILINE,
)

# Max context size cap (characters) for architecture notes and import graph
_MAX_CONTEXT_SIZE: int = 5000


class ModuleBoundary(BaseModel):
    """A detected module boundary in the repository.

    Args:
        path: Relative path to the module directory.
        purpose: Inferred purpose of the module.
        entry_points: List of detected entry point files.
    """

    path: str
    purpose: str
    entry_points: list[str] = Field(default_factory=list)


class APIContract(BaseModel):
    """A detected API contract in the repository.

    Args:
        file: File path where the API contract was detected.
        contract_type: Type of contract detected.
        description: Human-readable description of the contract.
    """

    file: str
    contract_type: str
    description: str


class OwnershipHint(BaseModel):
    """A CODEOWNERS pattern and its owners.

    Args:
        pattern: File glob pattern from CODEOWNERS.
        owners: List of GitHub usernames or team slugs.
    """

    pattern: str
    owners: list[str] = Field(default_factory=list)


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
    modules: list[ModuleBoundary] = Field(default_factory=list)
    api_contracts: list[APIContract] = Field(default_factory=list)
    ownership: list[OwnershipHint] = Field(default_factory=list)
    architecture_notes: list[str] = Field(default_factory=list)
    project_type: str = Field(default="unknown")
    import_graph_summary: str = Field(default="")
    repo_config: dict = Field(default_factory=dict)


class RepoScanner:
    """Scans a GitHub repository to detect conventions."""

    def __init__(self, github_client: GitHubAPIClient) -> None:
        self._client = github_client

    async def scan(self, owner: str, repo: str) -> RepoContext:
        """Scan a repository and return detected conventions.

        Args:
            owner: Repository owner.
            repo: Repository name.

        Returns:
            RepoContext with all detected conventions and structure.
        """
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

        # New detection methods — all wrapped in try/except for resilience
        try:
            modules = await self._detect_modules(owner, repo, root_contents)
        except (httpx.HTTPStatusError, KeyError, TypeError) as exc:
            logger.warning("Failed to detect modules for %s/%s: %s", owner, repo, exc)
            modules = []

        try:
            api_contracts = await self._detect_api_contracts(owner, repo, modules)
        except (httpx.HTTPStatusError, KeyError, TypeError) as exc:
            logger.warning("Failed to detect API contracts for %s/%s: %s", owner, repo, exc)
            api_contracts = []

        try:
            ownership = await self._detect_ownership(owner, repo, root_contents)
        except (httpx.HTTPStatusError, KeyError) as exc:
            logger.warning("Failed to detect ownership for %s/%s: %s", owner, repo, exc)
            ownership = []

        try:
            architecture_notes = await self._parse_readme_architecture(owner, repo)
        except (httpx.HTTPStatusError, re.error, KeyError) as exc:
            logger.warning(
                "Failed to parse README architecture for %s/%s: %s", owner, repo, exc,
            )
            architecture_notes = []

        try:
            import_graph_summary = await self._analyze_import_graph(
                owner, repo, modules,
            )
        except (httpx.HTTPStatusError, KeyError, IndexError, ValueError) as exc:
            logger.warning(
                "Failed to analyze import graph for %s/%s: %s", owner, repo, exc,
            )
            import_graph_summary = ""

        project_type = self._detect_project_type(root_contents, modules)

        try:
            repo_config = await self._read_repo_config(owner, repo) or {}
        except (httpx.HTTPStatusError, yaml.YAMLError, KeyError, ValueError) as exc:
            logger.warning("Failed to read repo config for %s/%s: %s", owner, repo, exc)
            repo_config = {}

        return RepoContext(
            languages=sorted(set(languages)),
            frameworks=sorted(set(frameworks)),
            has_tests=has_tests,
            test_frameworks=sorted(set(test_frameworks)),
            has_ci=bool(ci_systems),
            ci_systems=sorted(set(ci_systems)),
            has_linting=bool(linters),
            linters=sorted(set(linters)),
            modules=modules,
            api_contracts=api_contracts,
            ownership=ownership,
            architecture_notes=architecture_notes,
            project_type=project_type,
            import_graph_summary=import_graph_summary,
            repo_config=repo_config,
        )

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

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
            logger.debug("HTTP error reading file %s in %s/%s", path, owner, repo)
            return None
        except (UnicodeDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Unexpected error reading file %s in %s/%s",
                path, owner, repo, exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Original detection methods
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Module boundary detection
    # ------------------------------------------------------------------

    async def _detect_modules(
        self,
        owner: str,
        repo: str,
        root_contents: list[dict],
    ) -> list[ModuleBoundary]:
        """Scan directories for module boundaries (max 2 levels deep).

        Args:
            owner: Repository owner.
            repo: Repository name.
            root_contents: Already-fetched root directory listing.

        Returns:
            List of detected module boundaries.
        """
        modules: list[ModuleBoundary] = []
        root_dirs = [
            item for item in root_contents if item.get("type") == "dir"
        ]

        for dir_item in root_dirs:
            dir_name = dir_item["name"]
            # Skip hidden directories and common non-module dirs
            if dir_name.startswith(".") or dir_name in {
                "node_modules", "vendor", ".git", "__pycache__", "dist", "build",
            }:
                continue

            dir_contents = await self._list_dir(owner, repo, dir_name)
            if dir_contents is None:
                continue

            dir_file_names = {
                item["name"] for item in dir_contents
            }

            entry_points = sorted(dir_file_names & _ENTRY_POINT_FILES)
            if entry_points:
                purpose = self._infer_purpose(dir_name)
                modules.append(ModuleBoundary(
                    path=dir_name,
                    purpose=purpose,
                    entry_points=entry_points,
                ))

            # Scan second level
            sub_dirs = [
                item for item in dir_contents if item.get("type") == "dir"
            ]
            for sub_item in sub_dirs:
                sub_name = sub_item["name"]
                if sub_name.startswith(".") or sub_name in {
                    "node_modules", "__pycache__", "dist", "build",
                }:
                    continue

                sub_path = f"{dir_name}/{sub_name}"
                sub_contents = await self._list_dir(owner, repo, sub_path)
                if sub_contents is None:
                    continue

                sub_file_names = {
                    item["name"] for item in sub_contents
                }
                sub_entry_points = sorted(sub_file_names & _ENTRY_POINT_FILES)
                if sub_entry_points:
                    purpose = self._infer_purpose(sub_name)
                    modules.append(ModuleBoundary(
                        path=sub_path,
                        purpose=purpose,
                        entry_points=sub_entry_points,
                    ))

        return modules

    @staticmethod
    def _infer_purpose(dir_name: str) -> str:
        """Infer the purpose of a directory from its name.

        Args:
            dir_name: Name of the directory.

        Returns:
            Inferred purpose string.
        """
        return _PURPOSE_MAP.get(dir_name.lower(), "unknown")

    # ------------------------------------------------------------------
    # API contract detection
    # ------------------------------------------------------------------

    async def _detect_api_contracts(
        self,
        owner: str,
        repo: str,
        modules: list[ModuleBoundary],
    ) -> list[APIContract]:
        """Detect API contracts (FastAPI/Flask routes, GraphQL, protobuf, OpenAPI).

        Args:
            owner: Repository owner.
            repo: Repository name.
            modules: Already-detected module boundaries.

        Returns:
            List of detected API contracts.
        """
        contracts: list[APIContract] = []

        # Scan module entry points for route decorators
        for module in modules:
            for entry in module.entry_points:
                file_path = f"{module.path}/{entry}"
                content = await self._read_file(owner, repo, file_path)
                if content is None:
                    continue

                if _FASTAPI_PATTERN.search(content):
                    contracts.append(APIContract(
                        file=file_path,
                        contract_type="fastapi_route",
                        description=f"FastAPI routes in {file_path}",
                    ))
                elif _FLASK_PATTERN.search(content):
                    contracts.append(APIContract(
                        file=file_path,
                        contract_type="flask_route",
                        description=f"Flask routes in {file_path}",
                    ))

            # Scan module directory for special API files
            dir_contents = await self._list_dir(owner, repo, module.path)
            if dir_contents is None:
                continue

            for item in dir_contents:
                name = item.get("name", "")
                item_path = f"{module.path}/{name}"

                if name.endswith(".graphql") or name.endswith(".gql"):
                    contracts.append(APIContract(
                        file=item_path,
                        contract_type="graphql_schema",
                        description=f"GraphQL schema in {item_path}",
                    ))
                elif name.endswith(".proto"):
                    contracts.append(APIContract(
                        file=item_path,
                        contract_type="protobuf",
                        description=f"Protobuf definition in {item_path}",
                    ))
                elif name in {
                    "openapi.json", "openapi.yaml", "openapi.yml",
                    "swagger.json", "swagger.yaml", "swagger.yml",
                }:
                    contracts.append(APIContract(
                        file=item_path,
                        contract_type="openapi_spec",
                        description=f"OpenAPI spec in {item_path}",
                    ))

        return contracts

    # ------------------------------------------------------------------
    # Ownership detection
    # ------------------------------------------------------------------

    async def _detect_ownership(
        self,
        owner: str,
        repo: str,
        root_contents: list[dict],
    ) -> list[OwnershipHint]:
        """Parse CODEOWNERS file if present.

        Args:
            owner: Repository owner.
            repo: Repository name.
            root_contents: Already-fetched root directory listing.

        Returns:
            List of parsed ownership hints.
        """
        # CODEOWNERS can be at root, .github/, or docs/
        codeowners_paths = [
            "CODEOWNERS",
            ".github/CODEOWNERS",
            "docs/CODEOWNERS",
        ]

        root_names = {item["name"] for item in root_contents}

        for path in codeowners_paths:
            # Quick check: skip if the first segment isn't in root
            first_segment = path.split("/")[0]
            if first_segment not in root_names:
                continue

            content = await self._read_file(owner, repo, path)
            if content is not None:
                return self._parse_codeowners(content)

        return []

    @staticmethod
    def _parse_codeowners(content: str) -> list[OwnershipHint]:
        """Parse CODEOWNERS file content into ownership hints.

        Args:
            content: Raw CODEOWNERS file content.

        Returns:
            List of OwnershipHint entries.
        """
        hints: list[OwnershipHint] = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                pattern = parts[0]
                owners = [p for p in parts[1:] if not p.startswith("#")]
                if owners:
                    hints.append(OwnershipHint(
                        pattern=pattern,
                        owners=owners,
                    ))
        return hints

    # ------------------------------------------------------------------
    # README architecture extraction
    # ------------------------------------------------------------------

    async def _parse_readme_architecture(
        self,
        owner: str,
        repo: str,
    ) -> list[str]:
        """Extract architecture sections from README.md.

        Args:
            owner: Repository owner.
            repo: Repository name.

        Returns:
            List of architecture note strings.
        """
        content = await self._read_file(owner, repo, "README.md")
        if content is None:
            return []

        notes: list[str] = []
        total_chars = 0

        matches = list(_ARCHITECTURE_HEADERS.finditer(content))
        for match in matches:
            start = match.end()
            # Find the next header of same or higher level
            next_header = re.search(
                r"^#{1,3}\s+",
                content[start:],
                re.MULTILINE,
            )
            end = start + next_header.start() if next_header else len(content)
            section_text = content[start:end].strip()

            if section_text:
                # Cap total context size
                if total_chars + len(section_text) > _MAX_CONTEXT_SIZE:
                    remaining = _MAX_CONTEXT_SIZE - total_chars
                    if remaining > 0:
                        notes.append(section_text[:remaining])
                    break
                notes.append(section_text)
                total_chars += len(section_text)

        return notes

    # ------------------------------------------------------------------
    # Import graph analysis
    # ------------------------------------------------------------------

    async def _analyze_import_graph(
        self,
        owner: str,
        repo: str,
        modules: list[ModuleBoundary],
    ) -> str:
        """Scan key files for import patterns and detect cycles.

        Args:
            owner: Repository owner.
            repo: Repository name.
            modules: Already-detected module boundaries.

        Returns:
            Human-readable summary of the import dependency graph.
        """
        if not modules:
            return ""

        # Build adjacency list from imports in entry points
        # Map module path -> set of module paths it imports from
        module_paths = {m.path for m in modules}
        edges: dict[str, set[str]] = {m.path: set() for m in modules}

        for module in modules:
            for entry in module.entry_points:
                file_path = f"{module.path}/{entry}"
                content = await self._read_file(owner, repo, file_path)
                if content is None:
                    continue

                # Extract import targets
                for line in content.splitlines():
                    line = line.strip()
                    if not line.startswith(("import ", "from ")):
                        continue

                    # Convert dotted import to path-like form
                    import_target = self._extract_import_target(line)
                    if not import_target:
                        continue

                    # Check if import target maps to a known module
                    for target_path in module_paths:
                        if target_path == module.path:
                            continue
                        # Normalize for comparison (e.g., review_bot/review
                        # matches "review_bot.review")
                        normalized = target_path.replace("/", ".")
                        if import_target.startswith(normalized):
                            edges[module.path].add(target_path)

        # Detect cycles using DFS
        cycles = self._find_cycles(edges)

        # Build summary
        lines: list[str] = []
        dep_count = sum(len(deps) for deps in edges.values())
        lines.append(
            f"{len(modules)} modules, {dep_count} dependencies detected."
        )

        # List key dependencies
        for mod_path, deps in sorted(edges.items()):
            if deps:
                dep_list = ", ".join(sorted(deps))
                lines.append(f"  {mod_path} -> {dep_list}")

        if cycles:
            cycle_strs = [" -> ".join(c) for c in cycles]
            lines.append(f"Circular dependencies: {'; '.join(cycle_strs)}")

        summary = "\n".join(lines)
        return summary[:_MAX_CONTEXT_SIZE]

    @staticmethod
    def _extract_import_target(line: str) -> str:
        """Extract the module path from a Python import statement.

        Args:
            line: A stripped line starting with 'import' or 'from'.

        Returns:
            Dotted module path, or empty string if not parseable.
        """
        # "from foo.bar import baz" -> "foo.bar"
        # "import foo.bar" -> "foo.bar"
        match = re.match(r"from\s+([\w.]+)", line)
        if match:
            return match.group(1)
        match = re.match(r"import\s+([\w.]+)", line)
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _find_cycles(edges: dict[str, set[str]]) -> list[list[str]]:
        """Find cycles in a directed graph using DFS.

        Args:
            edges: Adjacency list mapping node -> set of neighbors.

        Returns:
            List of cycles, each represented as a list of node names.
        """
        cycles: list[list[str]] = []
        visited: set[str] = set()
        rec_stack: set[str] = set()
        path: list[str] = []

        def dfs(node: str) -> None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in sorted(edges.get(node, set())):
                if neighbor not in visited:
                    dfs(neighbor)
                elif neighbor in rec_stack:
                    # Found a cycle
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    cycles.append(cycle)

            path.pop()
            rec_stack.discard(node)

        for node in sorted(edges):
            if node not in visited:
                dfs(node)

        return cycles

    # ------------------------------------------------------------------
    # Project type detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_project_type(
        root_contents: list[dict],
        modules: list[ModuleBoundary],
    ) -> str:
        """Heuristically detect the project type.

        Args:
            root_contents: Root directory listing.
            modules: Already-detected module boundaries.

        Returns:
            One of: 'monorepo', 'microservice', 'library', 'application',
            'monolith', or 'unknown'.
        """
        root_names = {item["name"] for item in root_contents}
        root_dirs = {
            item["name"] for item in root_contents
            if item.get("type") == "dir"
        }

        # Monorepo indicators: lerna.json, pnpm-workspace.yaml, packages/ dir,
        # multiple go.mod-like configs, or workspaces
        monorepo_markers = {
            "lerna.json", "pnpm-workspace.yaml", "nx.json", "turbo.json",
        }
        if root_names & monorepo_markers:
            return "monorepo"
        if "packages" in root_dirs or "apps" in root_dirs:
            return "monorepo"

        # Library indicators: setup.py/pyproject.toml + src/ layout, or
        # package.json without server files
        library_markers = {"setup.py", "setup.cfg"}
        if root_names & library_markers and "src" in root_dirs:
            return "library"
        if "Cargo.toml" in root_names and "src" in root_dirs:
            # Check if it has a lib.rs (library) vs main.rs (application)
            # For now, default to library for Rust with src/
            return "library"

        # Microservice indicators: Dockerfile + small module count
        if "Dockerfile" in root_names and len(modules) <= 5:
            return "microservice"

        # Application indicators: manage.py (Django), main entry points
        app_markers = {"manage.py", "Procfile", "app.py", "main.py"}
        if root_names & app_markers:
            return "application"

        # Monolith: many modules, no clear separation
        if len(modules) > 10:
            return "monolith"

        return "unknown"

    # ------------------------------------------------------------------
    # Repo config reader
    # ------------------------------------------------------------------

    async def load_repo_config(self, owner: str, repo: str) -> RepoConfig:
        """Load and validate per-repo review config from .review-like-him.yml.

        Returns RepoConfig.default() on 404, malformed YAML, or any error.

        Args:
            owner: Repository owner.
            repo: Repository name.

        Returns:
            Validated RepoConfig instance, or default on failure.
        """
        content = await self._read_file(owner, repo, ".review-like-him.yml")
        if content is None:
            return RepoConfig.default()

        try:
            return RepoConfig.from_yaml(content)
        except (yaml.YAMLError, ValidationError, ValueError) as exc:
            logger.warning(
                "Failed to parse .review-like-him.yml in %s/%s, using defaults",
                owner, repo, exc_info=True,
            )
            return RepoConfig.default()

    async def _read_repo_config(
        self,
        owner: str,
        repo: str,
    ) -> dict | None:
        """Read .review-like-him.yml from repo root.

        Args:
            owner: Repository owner.
            repo: Repository name.

        Returns:
            Parsed YAML config as a dict, or None if not found/invalid.
        """
        content = await self._read_file(owner, repo, ".review-like-him.yml")
        if content is None:
            return None

        try:
            config = yaml.safe_load(content)
            if isinstance(config, dict):
                return config
            return None
        except (yaml.YAMLError, ValueError) as exc:
            logger.warning(
                "Failed to parse .review-like-him.yml in %s/%s: %s",
                owner, repo, exc,
            )
            return None

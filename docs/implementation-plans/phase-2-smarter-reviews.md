# Phase 2 — Smarter Reviews: Implementation Plan

> Last updated: 2026-03-15

This document provides exhaustive implementation details for all 6 Phase 2 roadmap items. Each section specifies file-by-file changes, function signatures, data model changes, prompt engineering details, error scenarios, and testing approach.

---

## Table of Contents

1. [Multi-pass review for large PRs](#1-multi-pass-review-for-large-prs)
2. [Context-aware reviews](#2-context-aware-reviews)
3. [Learning from review feedback](#3-learning-from-review-feedback)
4. [Confidence scores on comments](#4-confidence-scores-on-comments)
5. [Severity-based filtering](#5-severity-based-filtering)
6. [File-type-aware review strategies](#6-file-type-aware-review-strategies)

---

## 1. Multi-pass review for large PRs

**Complexity:** 🔴 Large
**Goal:** Replace the current summary-only fallback for large PRs (`LARGE_PR_FILE_THRESHOLD = 500` in `orchestrator.py`) with a multi-pass chunked review that reviews each partition independently and merges results.

### 1.1 Current Behavior

`ReviewOrchestrator.run_review()` checks `len(files) > LARGE_PR_FILE_THRESHOLD` (500) and delegates to `_handle_large_pr()`, which posts a file-count summary comment and returns a verdict of `"comment"` with no actual review findings. This means PRs with 500+ files get zero substantive review.

### 1.2 Diff Partitioning Strategy

#### New file: `review_bot/review/chunker.py`

```python
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from review_bot.github.api import PullRequestFile

logger = logging.getLogger("review-bot")

# Target maximum diff chars per chunk (aligned with MAX_DIFF_CHARS in prompt_builder.py)
DEFAULT_CHUNK_MAX_CHARS: int = 70_000

# Maximum files per chunk to keep LLM context manageable
DEFAULT_CHUNK_MAX_FILES: int = 50

# Size threshold for individual files that are "too large" to include fully
INDIVIDUAL_FILE_MAX_CHARS: int = 30_000


@dataclass
class DiffChunk:
    """A partition of the PR diff for independent review."""

    chunk_id: int
    label: str  # Human-readable label, e.g. "backend/api (Python)"
    files: list[PullRequestFile]
    diff_text: str
    directory_group: str  # Top-level directory or "root"
    file_type_group: str  # Primary file extension group


@dataclass
class ChunkingResult:
    """Result of partitioning a PR diff into reviewable chunks."""

    chunks: list[DiffChunk]
    skipped_files: list[str] = field(default_factory=list)  # Files excluded from all chunks
    total_files: int = 0


class DiffChunker:
    """Partitions large PR diffs into logical, reviewable chunks."""

    def __init__(
        self,
        max_chars_per_chunk: int = DEFAULT_CHUNK_MAX_CHARS,
        max_files_per_chunk: int = DEFAULT_CHUNK_MAX_FILES,
    ) -> None:
        self._max_chars = max_chars_per_chunk
        self._max_files = max_files_per_chunk

    def chunk(
        self,
        diff: str,
        files: list[PullRequestFile],
    ) -> ChunkingResult:
        """Partition diff into chunks using a multi-level grouping strategy.

        Grouping priority:
        1. By top-level directory (e.g., src/, tests/, docs/)
        2. Within directory, by file type (e.g., .py, .ts, .sql)
        3. If a directory group exceeds size limits, split further by subdirectory
        4. If a single file exceeds INDIVIDUAL_FILE_MAX_CHARS, truncate it

        Returns:
            ChunkingResult with ordered chunks and any skipped files.
        """

    def _split_diff_by_file(self, diff: str) -> dict[str, str]:
        """Split unified diff into per-file sections keyed by filename."""

    def _group_files_by_directory(
        self,
        files: list[PullRequestFile],
    ) -> dict[str, list[PullRequestFile]]:
        """Group files by their top-level directory."""

    def _split_oversized_group(
        self,
        directory: str,
        files: list[PullRequestFile],
        file_diffs: dict[str, str],
    ) -> list[DiffChunk]:
        """Split a directory group that exceeds chunk limits into sub-chunks."""

    def _truncate_large_file_diff(self, file_diff: str) -> str:
        """Truncate an individual file diff that exceeds INDIVIDUAL_FILE_MAX_CHARS.

        Keeps the first and last portions with a clear truncation marker.
        """

    @staticmethod
    def _classify_file_type(filename: str) -> str:
        """Classify a file into a type group (e.g., 'python', 'test', 'config', 'migration')."""

    @staticmethod
    def _is_generated_or_vendored(filename: str) -> bool:
        """Detect generated or vendored files to exclude from review chunks."""
```

#### Grouping algorithm detail

1. Parse the unified diff into per-file sections using `diff --git` markers (same logic as `PromptBuilder._split_diff()`, extracted to shared utility).
2. Group `PullRequestFile` objects by top-level directory (`src/`, `tests/`, `docs/`, `migrations/`, root-level files → `"root"`).
3. For each directory group, calculate total diff size. If under `max_chars_per_chunk` and under `max_files_per_chunk`, emit as a single chunk.
4. If a group exceeds limits, sub-group by file extension, then split further by subdirectory if needed.
5. Generated files (`*.min.js`, `*.generated.*`, `vendor/`, `node_modules/`, lock files) are excluded and listed in `skipped_files`.
6. Each chunk gets a human-readable label: `"{directory} ({primary_language})"`.

### 1.3 Shared Context Injection

Each chunk review prompt includes a **cross-chunk context header** so the LLM understands the full PR scope even when reviewing a single partition.

#### Cross-chunk context template (added to `PromptBuilder`)

```python
CROSS_CHUNK_CONTEXT_TEMPLATE = """\
## Cross-Chunk Context

This is chunk {chunk_number} of {total_chunks} in a multi-pass review of a \
large PR ({total_files} files).

**All chunks in this PR:**
{chunk_summary}

**Current chunk:** {current_chunk_label}
**Files in this chunk:** {chunk_file_count}

You are reviewing ONLY the files in this chunk. Other chunks are being \
reviewed separately. Focus on issues within these files, but note any \
cross-cutting concerns (e.g., "this change may affect the API contract \
in src/api/routes.py which is in another chunk").
"""
```

#### New method in `PromptBuilder`

```python
def build_chunked(
    self,
    persona: PersonaProfile,
    repo_context: RepoContext,
    pr_data: dict,
    chunk: DiffChunk,
    all_chunks: list[DiffChunk],
) -> str:
    """Build a prompt for a single chunk of a multi-pass review.

    Includes cross-chunk context header before the diff section.
    """
```

### 1.4 Merging and Deduplicating Findings

#### New file: `review_bot/review/merger.py`

```python
from __future__ import annotations

import logging
from dataclasses import dataclass

from review_bot.review.formatter import (
    CategorySection,
    InlineComment,
    ReviewResult,
)

logger = logging.getLogger("review-bot")


@dataclass
class MergeConflict:
    """Records when two chunks produced conflicting findings."""

    chunk_ids: list[int]
    finding_a: str
    finding_b: str
    resolution: str  # "kept_both" | "kept_higher_severity" | "deduplicated"


class ChunkResultMerger:
    """Merges ReviewResults from multiple chunk passes into a single result."""

    def merge(
        self,
        chunk_results: list[ReviewResult],
        chunk_labels: list[str],
    ) -> ReviewResult:
        """Merge multiple chunk ReviewResults into a unified ReviewResult.

        Steps:
        1. Collect all summary sections, grouping by category title.
        2. Deduplicate findings within each category using fuzzy matching.
        3. Merge inline comments, deduplicating by (file, line).
        4. Determine final verdict (most severe wins).
        5. Rank merged findings by severity.

        Returns:
            A single merged ReviewResult.
        """

    def _merge_sections(
        self,
        all_sections: list[tuple[str, CategorySection]],
    ) -> list[CategorySection]:
        """Merge sections across chunks, combining findings per category."""

    def _deduplicate_findings(
        self,
        findings: list[str],
    ) -> list[str]:
        """Remove near-duplicate findings using normalized text comparison.

        Two findings are considered duplicates if:
        - Their lowercased, whitespace-normalized text has >80% overlap (Jaccard similarity)
        - They reference the same file and similar line range
        """

    def _merge_inline_comments(
        self,
        all_comments: list[InlineComment],
    ) -> list[InlineComment]:
        """Merge inline comments, keeping the most detailed for duplicate (file, line) pairs."""

    def _resolve_verdict(
        self,
        verdicts: list[str],
    ) -> str:
        """Pick the most severe verdict: request_changes > comment > approve."""

    def _rank_by_severity(
        self,
        sections: list[CategorySection],
    ) -> list[CategorySection]:
        """Order sections by severity: Security > Bugs > Architecture > Performance > Testing > Style."""
```

### 1.5 Orchestrator Changes

#### File: `review_bot/review/orchestrator.py`

**Changes:**

1. Replace `_handle_large_pr()` with `_handle_large_pr_multipass()`.
2. Keep `LARGE_PR_FILE_THRESHOLD = 500` but change the behavior — instead of posting a summary, trigger multi-pass review.
3. Add a new threshold `MULTI_PASS_THRESHOLD = 80` — PRs between 80–500 files also benefit from chunking.
4. PRs with >1000 files still get a summary-only comment (practical upper limit for even multi-pass).

```python
# New constants
MULTI_PASS_THRESHOLD: int = 80  # Files above this trigger multi-pass
LARGE_PR_FILE_THRESHOLD: int = 500  # Kept for backward compat (now uses multi-pass)
EXTREME_PR_THRESHOLD: int = 1000  # PRs above this get summary-only

async def _handle_large_pr_multipass(
    self,
    owner: str,
    repo: str,
    pr_number: int,
    persona: PersonaProfile,
    pr_data: dict,
    diff: str,
    files: list[PullRequestFile],
    pr_url: str,
) -> ReviewResult:
    """Review a large PR by chunking the diff and reviewing each chunk.

    Steps:
    1. Partition diff using DiffChunker
    2. Build prompts for each chunk (with cross-chunk context)
    3. Execute reviews concurrently (with concurrency limit)
    4. Merge and deduplicate results
    5. Format and post
    """
```

**Updated `run_review()` flow:**

```python
# In run_review(), replace the current large PR check:
if len(files) > EXTREME_PR_THRESHOLD:
    # Still too large even for multi-pass
    return await self._handle_extreme_pr(...)

if len(files) > MULTI_PASS_THRESHOLD or len(diff) > MAX_DIFF_CHARS * 2:
    # Use multi-pass review
    return await self._handle_large_pr_multipass(...)

# Normal single-pass review continues below
```

**Concurrency for chunk reviews:**

```python
async def _review_chunks_concurrent(
    self,
    prompts: list[str],
    max_concurrent: int = 3,
) -> list[str]:
    """Execute chunk reviews with a concurrency semaphore."""
```

### 1.6 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| **Circular dependencies across chunks** | Cross-chunk context header lists all chunks and their files. LLM is instructed to note cross-cutting concerns without resolving them. Merger preserves cross-reference comments verbatim. |
| **Conflicting findings from different passes** | `ChunkResultMerger._deduplicate_findings()` uses Jaccard similarity; if two findings reference the same file/concept but have opposite conclusions, both are kept with a `⚠️ Cross-chunk conflict` prefix. |
| **Chunks that are too large individually** | `DiffChunker._split_oversized_group()` recursively splits by subdirectory, then by individual file. Files exceeding `INDIVIDUAL_FILE_MAX_CHARS` are truncated with a marker. |
| **Empty chunks** | `DiffChunker.chunk()` filters out chunks with empty `diff_text`. If all files in a group are generated/skipped, the group produces no chunk. |
| **Files spanning multiple logical groups** | Files are assigned to exactly one group (their top-level directory). Shared/util files in `"root"` are their own group. |
| **LARGE_PR_FILE_THRESHOLD backward compat** | Constant remains at 500 but now triggers multi-pass instead of summary-only. New `EXTREME_PR_THRESHOLD` at 1000 replaces the old summary-only behavior. |
| **Single chunk after partitioning** | If partitioning produces only 1 chunk, skip cross-chunk context and use the normal single-pass flow. |

### 1.7 Testing Approach

#### File: `tests/test_chunker.py`

- `test_chunk_small_pr_returns_single_chunk` — PR with 10 files produces 1 chunk.
- `test_chunk_groups_by_directory` — Files in `src/`, `tests/`, `docs/` produce 3 chunks.
- `test_chunk_splits_oversized_directory` — Directory with 100 files and large diff is split into sub-chunks.
- `test_chunk_excludes_generated_files` — `*.min.js`, lock files, `vendor/` excluded from chunks.
- `test_chunk_truncates_large_individual_file` — 50K-char file diff is truncated with marker.
- `test_chunk_handles_empty_diff` — Empty diff produces no chunks.

#### File: `tests/test_merger.py`

- `test_merge_deduplicates_similar_findings` — Two chunks finding "missing null check in api.py" merged into one.
- `test_merge_keeps_conflicting_findings` — Contradictory findings from different chunks both preserved.
- `test_merge_verdict_most_severe_wins` — `approve` + `request_changes` → `request_changes`.
- `test_merge_inline_comments_deduplicated_by_file_line` — Same (file, line) keeps most detailed comment.
- `test_merge_ranks_sections_by_severity` — Security sections appear before Style sections.

#### File: `tests/test_orchestrator.py` (additions)

- `test_multipass_triggered_for_large_pr` — PR with 100 files triggers multi-pass path.
- `test_extreme_pr_gets_summary_only` — PR with 1500 files gets summary comment.
- `test_multipass_concurrent_reviews` — Mock `ClaudeReviewer.review()` called once per chunk.

---

## 2. Context-aware reviews

**Complexity:** 🔴 Large
**Goal:** Extend `RepoScanner` to understand repository architecture — module boundaries, data flow patterns, API contracts, and ownership — and inject this context into review prompts.

### 2.1 Current Behavior

`RepoScanner.scan()` detects: languages, frameworks, test frameworks, CI systems, and linters. It reads root-level marker files and `pyproject.toml`/`package.json`. The resulting `RepoContext` is formatted by `PromptBuilder._format_repo_context()` as a bullet list of detected tools.

This tells the LLM *what* tools are used but not *how* the codebase is structured.

### 2.2 Extended RepoContext Model

#### File: `review_bot/review/repo_scanner.py` (modifications)

```python
class ModuleBoundary(BaseModel):
    """A detected module/package boundary in the repository."""

    path: str = Field(description="Directory path relative to repo root")
    purpose: str = Field(description="Inferred purpose: 'api', 'models', 'services', 'utils', 'tests', 'config', 'migrations', 'docs'")
    entry_points: list[str] = Field(default_factory=list, description="Key files (e.g., __init__.py, index.ts, mod.rs)")


class APIContract(BaseModel):
    """A detected API contract (endpoint, schema, or interface)."""

    file: str = Field(description="File defining the contract")
    contract_type: str = Field(description="'rest_endpoint', 'graphql', 'grpc', 'event', 'internal_interface'")
    description: str = Field(description="Brief description of the contract")


class OwnershipHint(BaseModel):
    """Ownership signal from CODEOWNERS or directory convention."""

    pattern: str = Field(description="File glob pattern")
    owners: list[str] = Field(default_factory=list, description="GitHub usernames or team slugs")


# Extended RepoContext with new fields:
class RepoContext(BaseModel):
    """Auto-detected repository conventions and context."""

    # Existing fields (unchanged)
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    has_tests: bool = False
    test_frameworks: list[str] = Field(default_factory=list)
    has_ci: bool = False
    ci_systems: list[str] = Field(default_factory=list)
    has_linting: bool = False
    linters: list[str] = Field(default_factory=list)

    # New fields
    modules: list[ModuleBoundary] = Field(default_factory=list)
    api_contracts: list[APIContract] = Field(default_factory=list)
    ownership: list[OwnershipHint] = Field(default_factory=list)
    architecture_notes: list[str] = Field(default_factory=list, description="Freeform notes from README/docs parsing")
    project_type: str = Field(default="unknown", description="'monorepo', 'microservice', 'library', 'application', 'monolith'")
    import_graph_summary: str = Field(default="", description="Summary of dependency relationships between modules")
```

### 2.3 New Scanner Methods

#### File: `review_bot/review/repo_scanner.py` (additions)

```python
class RepoScanner:
    # ... existing methods ...

    async def _detect_modules(
        self,
        owner: str,
        repo: str,
        root_contents: list[dict],
    ) -> list[ModuleBoundary]:
        """Detect module boundaries from directory structure.

        Heuristics:
        - Directories with __init__.py (Python packages)
        - Directories with index.ts/index.js (Node packages)
        - Directories with mod.rs (Rust modules)
        - Common convention directories: src/, lib/, app/, pkg/, internal/, cmd/

        Purpose inference based on directory name:
        - api/, routes/, endpoints/, handlers/ → 'api'
        - models/, schemas/, entities/ → 'models'
        - services/, usecases/, domain/ → 'services'
        - utils/, helpers/, common/, shared/ → 'utils'
        - tests/, test/, spec/, __tests__/ → 'tests'
        - config/, settings/, conf/ → 'config'
        - migrations/, alembic/ → 'migrations'
        - docs/, documentation/ → 'docs'
        """

    async def _detect_api_contracts(
        self,
        owner: str,
        repo: str,
        modules: list[ModuleBoundary],
    ) -> list[APIContract]:
        """Detect API contracts from known patterns.

        Detection strategies:
        - FastAPI/Flask/Express route files → REST endpoints
        - GraphQL schema files (*.graphql, schema.py) → GraphQL
        - Proto files (*.proto) → gRPC
        - OpenAPI/Swagger spec files → REST contracts
        - Pydantic BaseModel subclasses in schema files → internal interfaces
        """

    async def _detect_ownership(
        self,
        owner: str,
        repo: str,
        root_contents: list[dict],
    ) -> list[OwnershipHint]:
        """Parse CODEOWNERS file if present."""

    async def _parse_readme_architecture(
        self,
        owner: str,
        repo: str,
    ) -> list[str]:
        """Extract architecture notes from README.md.

        Looks for sections titled:
        - Architecture, Structure, Design, Overview, How it works
        Extracts the text content (limited to 2000 chars) as freeform notes.
        """

    async def _analyze_import_graph(
        self,
        owner: str,
        repo: str,
        modules: list[ModuleBoundary],
    ) -> str:
        """Build a simplified import graph summary.

        For Python: scan __init__.py and key files for `from X import Y` patterns.
        For JS/TS: scan for `import ... from '...'` patterns.

        Returns a human-readable summary like:
        'api → services → models (layered architecture, no circular deps detected)'
        """

    def _detect_project_type(
        self,
        root_contents: list[dict],
        modules: list[ModuleBoundary],
    ) -> str:
        """Infer project type from structure.

        Heuristics:
        - Multiple package.json/go.mod at different levels → 'monorepo'
        - Single entry point + Dockerfile → 'microservice'
        - setup.py/pyproject.toml with [project] → 'library'
        - Presence of Procfile/Dockerfile/docker-compose → 'application'
        - Catch-all → 'monolith'
        """
```

### 2.4 PromptBuilder Integration

#### File: `review_bot/review/prompt_builder.py` (modifications)

```python
def _format_repo_context(self, ctx: RepoContext) -> str:
    """Format repo context as readable text.

    Extended to include module boundaries, API contracts, and architecture notes.
    """
    lines = []
    # ... existing language/framework/test/CI/linter lines ...

    if ctx.project_type != "unknown":
        lines.append(f"- Project type: {ctx.project_type}")

    if ctx.modules:
        lines.append("\n**Module structure:**")
        for mod in ctx.modules[:10]:  # Cap at 10 to avoid prompt bloat
            lines.append(f"  - `{mod.path}/` — {mod.purpose}")

    if ctx.api_contracts:
        lines.append("\n**API contracts:**")
        for contract in ctx.api_contracts[:8]:
            lines.append(f"  - `{contract.file}` ({contract.contract_type}): {contract.description}")

    if ctx.architecture_notes:
        lines.append("\n**Architecture notes (from docs):**")
        for note in ctx.architecture_notes[:5]:
            # Truncate individual notes
            truncated = note[:500] + "..." if len(note) > 500 else note
            lines.append(f"  - {truncated}")

    if ctx.import_graph_summary:
        lines.append(f"\n**Dependency flow:** {ctx.import_graph_summary}")

    if ctx.ownership:
        lines.append("\n**Code ownership:**")
        for hint in ctx.ownership[:5]:
            lines.append(f"  - `{hint.pattern}` → {', '.join(hint.owners)}")

    return "\n".join(lines) + "\n\n" if lines else ""
```

### 2.5 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| **Monorepo vs microservice detection** | Presence of multiple `package.json`/`go.mod` at different directory depths signals monorepo. Single root config signals mono or micro. `Dockerfile` count helps disambiguate. |
| **Unconventional project structures** | Fallback to `project_type: "unknown"` and `modules: []`. The LLM still gets language/framework context from existing detection. |
| **Generated code directories** | `_is_generated_or_vendored()` check (shared with chunker): skip `vendor/`, `node_modules/`, `generated/`, `__generated__/`, `dist/`, `build/`. |
| **Vendored dependencies** | Detected by presence of `vendor/` directory or `vendor` in path. Excluded from module detection and import graph. |
| **README parsing failure** | `_parse_readme_architecture()` returns `[]` on any error. Uses try/except with `logger.warning()`. |
| **Import graph cycles** | `_analyze_import_graph()` detects cycles and includes them in the summary: `"⚠️ Circular dependency detected: api ↔ services"`. |
| **Large repos with deep nesting** | Module detection only scans 2 levels deep by default. Configurable via `max_scan_depth` parameter. |
| **Prompt size bloat** | All context sections are capped (10 modules, 8 contracts, 5 notes, 5 ownership rules). Total architecture context limited to 3000 chars. |

### 2.6 Testing Approach

#### File: `tests/test_repo_scanner.py` (new)

- `test_detect_modules_python_packages` — Mock repo with `src/api/__init__.py`, `src/models/__init__.py` → 2 modules with correct purposes.
- `test_detect_modules_javascript` — Mock repo with `src/components/index.ts` → module detected.
- `test_detect_api_contracts_fastapi` — Mock file content with `@app.get("/users")` → REST endpoint detected.
- `test_detect_ownership_codeowners` — Mock CODEOWNERS content parsed correctly.
- `test_parse_readme_architecture` — README with `## Architecture` section → notes extracted.
- `test_detect_project_type_monorepo` — Multiple `package.json` → `"monorepo"`.
- `test_detect_project_type_library` — `pyproject.toml` with `[project]` → `"library"`.
- `test_import_graph_detects_cycles` — Circular imports → warning in summary.
- `test_context_size_capped` — Large repo context truncated to stay within limits.

#### File: `tests/test_prompt_builder.py` (additions)

- `test_format_repo_context_with_modules` — Extended context includes module section.
- `test_format_repo_context_caps_entries` — >10 modules truncated to 10.

---

## 3. Learning from review feedback

**Complexity:** 🔴 Large
**Goal:** Track PR author reactions (👍/👎) and comment resolve/dismiss actions on bot review comments, then use this feedback to refine persona priorities over time.

### 3.1 Feedback Collection Strategy

#### Why not webhooks for reactions?

GitHub does **not** send webhook payloads when reactions are added to or removed from pull request review comments. The `pull_request_review_comment` webhook only fires for `created`, `edited`, and `deleted` actions on the comment itself — there is no "reaction" sub-event. Therefore, reaction data must be collected via **polling the GitHub REST API**.

#### Polling-based reaction collection

##### New file: `review_bot/review/feedback_poller.py`

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from review_bot.github.api import GitHubAPIClient

logger = logging.getLogger("review-bot")

# Reaction → feedback signal mapping
REACTION_FEEDBACK: dict[str, str] = {
    "+1": "positive",
    "-1": "negative",
    "confused": "negative",
    "hooray": "positive",
    "heart": "positive",
    "rocket": "positive",
    "laugh": "neutral",  # Ambiguous — ignored in scoring
    "eyes": "neutral",
}


@dataclass
class ReactionPollResult:
    """Result of polling reactions for a single comment."""

    comment_id: str
    reactions: list[dict]  # Raw reaction objects from GitHub API
    poll_timestamp: str  # ISO 8601


class FeedbackPoller:
    """Polls GitHub REST API for reactions on bot review comments.

    Runs on a configurable schedule (default: every 6 hours) and collects
    reactions on all tracked bot comments that are younger than
    `max_comment_age` (default: 30 days).
    """

    def __init__(
        self,
        github_client: GitHubAPIClient,
        feedback_store: FeedbackStore,
        poll_interval: timedelta = timedelta(hours=6),
        max_comment_age: timedelta = timedelta(days=30),
    ) -> None:
        self._client = github_client
        self._store = feedback_store
        self._poll_interval = poll_interval
        self._max_comment_age = max_comment_age

    async def poll_reactions_for_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
    ) -> list[dict]:
        """Fetch reactions for a single PR review comment via REST API.

        Uses: GET /repos/{owner}/{repo}/pulls/comments/{comment_id}/reactions
        Handles pagination for comments with many reactions.
        Returns list of reaction objects with 'content' and 'user' fields.
        """

    async def poll_all_tracked_comments(self) -> int:
        """Poll reactions for all tracked bot comments within max_comment_age.

        Returns the number of new feedback events recorded.

        Steps:
        1. Query review_comment_tracking for comments posted within max_comment_age.
        2. For each comment, call poll_reactions_for_comment().
        3. Diff against previously recorded reactions (stored in review_feedback).
        4. Insert new FeedbackEvents for any new reactions found.
        5. Handle deleted reactions by marking them as retracted.
        """

    async def run_poll_loop(self) -> None:
        """Long-running loop that polls on schedule.

        Called from the FastAPI lifespan as a background task:
        asyncio.create_task(poller.run_poll_loop())
        """
```

##### Scheduling in server lifespan

```python
# In review_bot/server/app.py lifespan:
from review_bot.review.feedback_poller import FeedbackPoller

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... existing startup ...
    poller = FeedbackPoller(
        github_client=app.state.github_client,
        feedback_store=app.state.feedback_store,
        poll_interval=timedelta(hours=settings.feedback_poll_interval_hours),
    )
    poll_task = asyncio.create_task(poller.run_poll_loop())
    yield
    poll_task.cancel()
    # ... existing shutdown ...
```

#### Webhook-based feedback (non-reaction events)

For events that GitHub **does** deliver via webhooks, we handle them directly:

##### File: `review_bot/server/webhooks.py` (modifications)

```python
# In webhook_handler(), add new event routing:
elif event == "pull_request_review_comment" and data.get("action") == "created":
    # Track replies to bot comments (potential feedback via reply text)
    await _handle_review_comment_reply(data)
elif event == "pull_request_review" and data.get("action") == "dismissed":
    # Track review dismissals (negative signal for all comments in that review)
    await _handle_review_dismissed(data)
```

**New handler functions:**

```python
async def _handle_review_comment_reply(data: dict) -> None:
    """Handle reply comments on bot review comments.

    If the reply is on a bot comment, analyze sentiment (e.g., "good catch"
    vs "this is wrong") as a lightweight feedback signal.
    """

async def _handle_review_dismissed(data: dict) -> None:
    """Handle pull_request_review dismissed events.

    When a review is dismissed, all its comments get negative feedback signal.
    """
```

#### Required GitHub App permission changes

The GitHub App must be configured with:
- `pull_request_review_comment` webhook events (already likely enabled)
- `pull_request_review` webhook events (for dismiss tracking)
- `reactions:read` permission on the GitHub App (for REST API polling)

### 3.2 Feedback Database Schema

#### New table: `review_feedback`

```sql
CREATE TABLE IF NOT EXISTS review_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL,          -- References reviews table
    comment_id TEXT NOT NULL,         -- GitHub comment ID
    comment_body TEXT NOT NULL,       -- The review comment text
    comment_category TEXT NOT NULL,   -- Category from review (Bugs, Style, etc.)
    persona_name TEXT NOT NULL,
    repo TEXT NOT NULL,               -- owner/repo
    pr_number INTEGER NOT NULL,
    feedback_type TEXT NOT NULL,      -- 'positive', 'negative', 'dismiss', 'resolve'
    feedback_source TEXT NOT NULL,    -- 'reaction', 'dismiss', 'resolve', 'reply'
    reactor_username TEXT NOT NULL,   -- Who gave the feedback
    is_pr_author BOOLEAN NOT NULL,   -- Whether reactor is the PR author
    created_at TEXT NOT NULL,         -- ISO 8601
    UNIQUE(comment_id, reactor_username, feedback_type)  -- Prevent double-counting
);

CREATE INDEX idx_feedback_persona ON review_feedback(persona_name);
CREATE INDEX idx_feedback_category ON review_feedback(persona_name, comment_category);
CREATE INDEX idx_feedback_created ON review_feedback(created_at);
```

#### New table: `review_comment_tracking`

```sql
CREATE TABLE IF NOT EXISTS review_comment_tracking (
    comment_id TEXT PRIMARY KEY,      -- GitHub comment ID
    review_id TEXT NOT NULL,          -- Our internal review ID
    persona_name TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    comment_body TEXT NOT NULL,
    comment_category TEXT NOT NULL,
    posted_at TEXT NOT NULL
);
```

### 3.3 Feedback Service

#### New file: `review_bot/review/feedback.py`

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("review-bot")


@dataclass
class FeedbackEvent:
    """A single feedback event on a review comment."""

    comment_id: str
    feedback_type: str  # 'positive', 'negative', 'dismiss', 'resolve'
    feedback_source: str  # 'reaction', 'dismiss', 'resolve', 'reply'
    reactor_username: str
    is_pr_author: bool


@dataclass
class FeedbackSummary:
    """Aggregated feedback stats for a persona's review category."""

    category: str
    positive_count: int
    negative_count: int
    total_comments: int
    approval_rate: float  # positive / total, 0.0-1.0
    sample_positive: list[str]  # Example comments that got positive feedback
    sample_negative: list[str]  # Example comments that got negative feedback


class FeedbackStore:
    """Stores and queries review feedback for persona refinement."""

    def __init__(self, db_engine: AsyncEngine) -> None:
        self._db = db_engine

    async def record_feedback(self, event: FeedbackEvent) -> None:
        """Record a feedback event, deduplicating by (comment_id, reactor, type)."""

    async def track_posted_comment(
        self,
        comment_id: str,
        review_id: str,
        persona_name: str,
        repo: str,
        pr_number: int,
        file_path: str,
        line_number: int,
        body: str,
        category: str,
    ) -> None:
        """Track a posted review comment for later feedback correlation."""

    async def get_persona_feedback_summary(
        self,
        persona_name: str,
        since_days: int = 90,
    ) -> list[FeedbackSummary]:
        """Get aggregated feedback per category for a persona.

        Only considers feedback from the last `since_days` days.
        Weights PR author feedback 2x compared to other users.
        """

    async def get_category_approval_rates(
        self,
        persona_name: str,
    ) -> dict[str, float]:
        """Get approval rate per category for priority re-weighting."""
```

### 3.4 Persona Re-analysis with Feedback

#### File: `review_bot/persona/analyzer.py` (modifications)

```python
async def reanalyze_with_feedback(
    self,
    persona_name: str,
    feedback_store: FeedbackStore,
) -> PersonaProfile:
    """Re-analyze a persona incorporating feedback scores.

    Algorithm:
    1. Load current persona profile.
    2. Get feedback summary per category.
    3. Adjust priority severity based on approval rates:
       - Category approval rate > 0.8 → keep or promote severity
       - Category approval rate 0.5-0.8 → keep current severity
       - Category approval rate 0.3-0.5 → demote one severity level
       - Category approval rate < 0.3 → demote two levels or add to 'nits_on'
    4. Apply exponential moving average (α=0.3) to prevent oscillation.
    5. Update persona profile with adjusted priorities.

    The EMA ensures that a single batch of negative feedback doesn't
    drastically change the persona — changes are gradual.
    """
```

#### Re-weighting stability (anti-oscillation)

```python
@dataclass
class CategoryWeight:
    """Tracks the smoothed weight for a category to prevent oscillation."""

    category: str
    raw_approval_rate: float
    smoothed_rate: float  # EMA-smoothed
    previous_smoothed: float
    adjustment: str  # 'promote', 'keep', 'demote', 'demote_2'


def _apply_ema_smoothing(
    current_rate: float,
    previous_rate: float,
    alpha: float = 0.3,
) -> float:
    """Exponential moving average to smooth feedback signals.

    alpha = 0.3 means 30% weight on new data, 70% on historical.
    Prevents a single bad batch of feedback from wildly swinging priorities.
    """
    return alpha * current_rate + (1 - alpha) * previous_rate
```

### 3.5 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| **Reaction spam** | `UNIQUE(comment_id, reactor_username, feedback_type)` constraint prevents counting multiple identical reactions. Rate-limit feedback ingestion to 100 events/minute per persona. |
| **Feedback on old reviews** | Feedback is timestamped; `get_persona_feedback_summary()` only considers last 90 days by default. Older feedback decays naturally through EMA. |
| **Deleted comments with reactions** | When a comment is deleted, its `review_comment_tracking` entry remains but new feedback is ignored (comment_id lookup fails). Existing feedback is retained for historical accuracy. |
| **Multiple reactions from same user** | Deduplicated by `UNIQUE(comment_id, reactor_username, feedback_type)`. If a user adds both 👍 and 👎 (unusual), both are recorded — net effect is zero. |
| **Feedback from non-PR-authors** | Recorded with `is_pr_author = False`. PR author feedback gets 2x weight in aggregation since they're the most relevant judge of review quality. |
| **Re-weighting oscillation** | EMA smoothing (α=0.3) prevents rapid swings. Additionally, a minimum sample size of 10 feedback events per category is required before any adjustment is applied. |
| **Bot reacting to itself** | Filter by checking `reactor_username` against known bot usernames. Ignore reactions from `*[bot]` accounts. |

### 3.6 Tracking Posted Comments for Feedback Correlation

#### File: `review_bot/review/github_poster.py` (modifications)

```python
class ReviewPoster:
    def __init__(
        self,
        github_client: GitHubAPIClient,
        feedback_store: FeedbackStore | None = None,  # New optional dependency
    ) -> None:
        self._client = github_client
        self._feedback_store = feedback_store

    async def post(self, ...) -> dict:
        """Post review and track comments for feedback correlation."""
        response = await self._client.post_review(...)

        # Track posted comments for later feedback
        if self._feedback_store and response.get("id"):
            review_id = str(response["id"])
            # GitHub returns comment IDs in the review response
            for comment_data in response.get("comments", []):
                await self._feedback_store.track_posted_comment(
                    comment_id=str(comment_data["id"]),
                    review_id=review_id,
                    # ... other fields
                )
```

### 3.7 Testing Approach

#### File: `tests/test_feedback.py` (new)

- `test_record_feedback_positive` — Record 👍 reaction, verify stored.
- `test_record_feedback_deduplicates` — Same user, same reaction, same comment → single record.
- `test_feedback_summary_by_category` — 5 positive + 2 negative on "Bugs" → 71% approval rate.
- `test_feedback_weights_pr_author_2x` — PR author feedback weighted double.
- `test_feedback_ignores_old_reviews` — Feedback from 120 days ago excluded from 90-day summary.
- `test_feedback_minimum_sample_size` — Category with < 10 feedback events → no adjustment.
- `test_ema_smoothing_prevents_oscillation` — Rapid approval rate changes are dampened.
- `test_ignore_bot_reactions` — Reactions from `*[bot]` accounts filtered out.

#### File: `tests/test_webhooks.py` (additions)

- `test_reaction_event_routes_to_handler` — Reaction webhook payload correctly routed.
- `test_review_dismissed_marks_negative` — Dismissed review creates negative feedback events.

---

## 4. Confidence scores on comments

**Complexity:** 🟡 Medium
**Goal:** Have the LLM rate each review comment with a confidence level (high/medium/low) and surface this in the formatted output.

### 4.1 LLM Prompt Modifications

#### File: `review_bot/review/prompt_builder.py` (modifications)

Update `SYSTEM_PROMPT_TEMPLATE` to request confidence ratings:

```python
# In the JSON schema section, update inline_comments format:
"""
  "inline_comments": [
    {{
      "file": "path/to/file.py",
      "line": 42,
      "body": "Your comment here",
      "confidence": "high",
      "confidence_reason": "This directly violates the error handling pattern used throughout the codebase"
    }}
  ]
"""

# Add confidence instructions to the Rules section:
"""
- For each inline comment, rate your confidence as:
  - **high**: You are very confident this is a real issue based on the persona's \
known preferences, clear code violations, or obvious bugs. The issue would be \
flagged by most experienced reviewers.
  - **medium**: You believe this is likely an issue but there may be context you're \
missing (e.g., a pattern used elsewhere in the codebase, a deliberate design choice). \
A reasonable reviewer might disagree.
  - **low**: This is speculative — you're noting it because it *could* be a problem \
but you don't have strong evidence. The persona *might* flag this based on their \
general tendencies.
- Include a brief confidence_reason explaining why you chose that level.
- Also rate confidence for each finding in summary_sections:
"""

# Update summary_sections schema:
"""
  "summary_sections": [
    {{
      "emoji": "🐛",
      "title": "Bugs",
      "findings": [
        {{
          "text": "Finding description here",
          "confidence": "high",
          "confidence_reason": "Clear null pointer dereference"
        }}
      ]
    }}
  ],
"""
```

### 4.2 Data Model Changes

#### File: `review_bot/review/formatter.py` (modifications)

```python
class Finding(BaseModel):
    """A single review finding with confidence metadata."""

    text: str = Field(description="Finding description")
    confidence: str = Field(
        default="medium",
        description="Confidence level: 'high', 'medium', 'low'",
    )
    confidence_reason: str = Field(
        default="",
        description="Brief explanation for the confidence rating",
    )


class CategorySection(BaseModel):
    """A categorized section of review findings with emoji prefix."""

    emoji: str = Field(description="Section emoji prefix")
    title: str = Field(description="Section title")
    findings: list[Finding] = Field(
        default_factory=list,
        description="List of findings with confidence metadata",
    )


class InlineComment(BaseModel):
    """A review comment attached to a specific file and line."""

    file: str = Field(description="File path relative to repo root")
    line: int = Field(description="Line number in the diff")
    body: str = Field(description="Comment text")
    confidence: str = Field(
        default="medium",
        description="Confidence level: 'high', 'medium', 'low'",
    )
    confidence_reason: str = Field(
        default="",
        description="Brief explanation for the confidence rating",
    )
```

**Migration note:** The `findings` field changes from `list[str]` to `list[Finding]`. This is a **breaking change** to `CategorySection`. All code referencing `section.findings` as strings must be updated to use `finding.text`. The `_from_json` parser must handle both old-format (plain strings) and new-format (objects with confidence) for backward compatibility.

### 4.3 Formatter Changes for Display

#### File: `review_bot/review/formatter.py` (modifications)

```python
# Confidence display prefixes
CONFIDENCE_PREFIXES: dict[str, str] = {
    "high": "🔴",    # High confidence → red circle (likely real issue)
    "medium": "🟡",  # Medium confidence → yellow circle
    "low": "⚪",     # Low confidence → white circle (speculative)
}


class ReviewFormatter:
    def _from_json(self, data: dict, persona_name: str, pr_url: str) -> ReviewResult:
        """Build ReviewResult from parsed JSON dict.

        Handles both old format (findings as strings) and new format
        (findings as objects with confidence).
        """
        # ... existing verdict parsing ...

        sections: list[CategorySection] = []
        for section_data in data.get("summary_sections", []):
            title = section_data.get("title", "")
            emoji = section_data.get("emoji", CATEGORY_EMOJIS.get(title, "📝"))
            raw_findings = section_data.get("findings", [])
            findings: list[Finding] = []
            for f in raw_findings:
                if isinstance(f, str):
                    # Backward compat: plain string → Finding with medium confidence
                    findings.append(Finding(text=f))
                elif isinstance(f, dict):
                    findings.append(Finding(
                        text=f.get("text", str(f)),
                        confidence=self._normalize_confidence(f.get("confidence", "medium")),
                        confidence_reason=f.get("confidence_reason", ""),
                    ))
            if findings:
                sections.append(CategorySection(emoji=emoji, title=title, findings=findings))

        # Similar update for inline_comments parsing...

    @staticmethod
    def _normalize_confidence(value: str) -> str:
        """Normalize confidence value to 'high', 'medium', or 'low'."""
        normalized = value.strip().lower()
        if normalized in ("high", "medium", "low"):
            return normalized
        # Map common LLM variations
        if normalized in ("very high", "certain", "definite"):
            return "high"
        if normalized in ("moderate", "somewhat", "likely"):
            return "medium"
        if normalized in ("very low", "uncertain", "speculative", "unsure"):
            return "low"
        return "medium"  # Default fallback
```

#### File: `review_bot/review/github_poster.py` (modifications)

Update `_format_body()` to display confidence:

```python
def _format_body(self, result: ReviewResult) -> str:
    """Format the review body with confidence indicators."""
    lines: list[str] = []
    lines.append(f"## Reviewing as {result.persona_name}-bot 🤖")
    lines.append("")
    # ... verdict badge ...

    for section in result.summary_sections:
        lines.append(f"### {section.emoji} {section.title}")
        lines.append("")
        for finding in section.findings:
            prefix = CONFIDENCE_PREFIXES.get(finding.confidence, "🟡")
            lines.append(f"- {prefix} {finding.text}")
        lines.append("")

    # Legend
    if result.summary_sections:
        lines.append("---")
        lines.append("*🔴 High confidence · 🟡 Medium · ⚪ Low (speculative)*")
        lines.append("")

    return "\n".join(lines)
```

For inline comments, prepend confidence prefix to the comment body:

```python
# In ReviewPoster.post(), modify inline comment formatting:
comments = [
    ReviewComment(
        path=ic.file,
        line=ic.line,
        body=f"{CONFIDENCE_PREFIXES.get(ic.confidence, '🟡')} {ic.body}",
    )
    for ic in result.inline_comments
]
```

### 4.4 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| **LLM refuses to rate confidence** | `_normalize_confidence()` defaults to `"medium"` for any unrecognized value. Missing `confidence` key in JSON also defaults to `"medium"`. |
| **Inconsistent ratings across similar issues** | This is acceptable — confidence reflects the LLM's certainty about each specific instance, which can legitimately vary. No post-hoc normalization. |
| **Calibration — are 'high' ratings reliable?** | Tracked via feedback system (item 3). If high-confidence comments consistently get 👎, the persona's priority weights are adjusted, not the confidence system itself. |
| **Impact on review usefulness** | Confidence scores are display-only metadata. They don't affect verdict, posting, or filtering (severity-based filtering in item 5 uses severity, not confidence). |
| **Old-format backward compatibility** | `_from_json()` handles both `findings: ["text"]` (old) and `findings: [{"text": "...", "confidence": "..."}]` (new). |
| **Prompt size increase** | Adding confidence instructions adds ~400 chars to the prompt. Well within limits. |

### 4.5 Testing Approach

#### File: `tests/test_formatter.py` (modifications/additions)

- `test_finding_model_defaults` — `Finding()` defaults to `confidence="medium"`.
- `test_normalize_confidence_valid_values` — `"high"`, `"medium"`, `"low"` pass through.
- `test_normalize_confidence_llm_variations` — `"very high"` → `"high"`, `"uncertain"` → `"low"`.
- `test_normalize_confidence_fallback` — Garbage input → `"medium"`.
- `test_parse_findings_old_format` — `["text1", "text2"]` → `[Finding(text="text1"), ...]`.
- `test_parse_findings_new_format` — `[{"text": "x", "confidence": "high"}]` parsed correctly.
- `test_format_body_includes_confidence_prefixes` — Output contains 🔴/🟡/⚪ markers.
- `test_format_body_includes_legend` — Output contains confidence legend line.
- `test_inline_comment_confidence_prefix` — Inline comment body prepended with confidence emoji.

---

## 5. Severity-based filtering

**Complexity:** 🟡 Medium
**Goal:** Only post review comments above a configurable severity threshold, reducing noise on busy repositories.

### 5.1 Settings Model Changes

#### File: `review_bot/config/settings.py` (modifications)

```python
from enum import IntEnum


class MinSeverity(IntEnum):
    """Minimum severity levels for filtering review comments."""

    ALL = 0           # Post everything
    LOW = 1           # Filter out trivial noise only
    MEDIUM = 2        # Only medium and above
    HIGH = 3          # Only high-severity issues
    CRITICAL = 4      # Only critical/blocking issues


class Settings(BaseSettings):
    # ... existing fields ...

    min_severity: int = Field(
        default=0,
        description="Global minimum severity (0=all, 1=low, 2=medium, 3=high, 4=critical)",
    )

    @field_validator("min_severity")
    @classmethod
    def _validate_min_severity(cls, v: int) -> int:
        """Severity must be in valid range."""
        if not (0 <= v <= 4):
            raise ValueError("min_severity must be between 0 and 4")
        return v
```

#### Per-repo override via `.review-like-him.yml`

While the Phase 3 roadmap item "Review templates per repo/team" handles full per-repo config, the severity filter adds a simple per-repo override mechanism now:

```python
# In RepoScanner, add:
async def _read_repo_config(
    self,
    owner: str,
    repo: str,
) -> dict | None:
    """Read .review-like-him.yml from repo root if present."""
    content = await self._read_file(owner, repo, ".review-like-him.yml")
    if content:
        import yaml
        try:
            return yaml.safe_load(content)
        except yaml.YAMLError:
            logger.warning("Invalid .review-like-him.yml in %s/%s", owner, repo)
    return None
```

Expected `.review-like-him.yml` format (relevant fields):

```yaml
min_severity: 2  # Only post medium and above
```

### 5.2 Severity Mapping

#### New file: `review_bot/review/severity.py`

```python
from __future__ import annotations

import logging
from collections.abc import Sequence

from review_bot.review.formatter import (
    CategorySection,
    Finding,
    InlineComment,
    ReviewResult,
)

logger = logging.getLogger("review-bot")

# Category → base severity mapping
CATEGORY_SEVERITY: dict[str, int] = {
    "Security": 4,       # Critical
    "Bugs": 3,           # High
    "Architecture": 2,   # Medium
    "Performance": 2,    # Medium
    "Testing": 1,        # Low
    "Style": 1,          # Low
}

# Confidence → severity modifier
CONFIDENCE_SEVERITY_BOOST: dict[str, int] = {
    "high": 1,    # Boost severity by 1 for high-confidence findings
    "medium": 0,
    "low": -1,    # Reduce severity by 1 for speculative findings
}


def compute_finding_severity(
    category: str,
    confidence: str = "medium",
) -> int:
    """Compute the effective severity of a finding.

    Combines category base severity with confidence modifier.
    Clamped to [0, 4] range.
    """
    base = CATEGORY_SEVERITY.get(category, 2)
    boost = CONFIDENCE_SEVERITY_BOOST.get(confidence, 0)
    return max(0, min(4, base + boost))


def filter_result_by_severity(
    result: ReviewResult,
    min_severity: int,
) -> ReviewResult:
    """Filter a ReviewResult, removing findings below the severity threshold.

    Args:
        result: The full review result from the LLM.
        min_severity: Minimum severity level (0-4).

    Returns:
        A new ReviewResult with only findings at or above the threshold.
        If all findings are filtered out, returns a result with
        verdict='approve' and an LGTM summary.
    """
    if min_severity <= 0:
        return result  # No filtering

    filtered_sections: list[CategorySection] = []
    for section in result.summary_sections:
        filtered_findings: list[Finding] = []
        for finding in section.findings:
            severity = compute_finding_severity(section.title, finding.confidence)
            if severity >= min_severity:
                filtered_findings.append(finding)

        if filtered_findings:
            filtered_sections.append(
                CategorySection(
                    emoji=section.emoji,
                    title=section.title,
                    findings=filtered_findings,
                )
            )

    filtered_inline: list[InlineComment] = []
    for comment in result.inline_comments:
        # Inline comments don't have a category — infer from body heuristics
        inferred_category = _infer_comment_category(comment.body)
        severity = compute_finding_severity(inferred_category, comment.confidence)
        if severity >= min_severity:
            filtered_inline.append(comment)

    # Determine what to do when everything is filtered
    if not filtered_sections and not filtered_inline:
        return _create_lgtm_result(result, min_severity)

    # Re-determine verdict based on filtered findings
    verdict = _recompute_verdict(result.verdict, filtered_sections, filtered_inline)

    return ReviewResult(
        verdict=verdict,
        summary_sections=filtered_sections,
        inline_comments=filtered_inline,
        persona_name=result.persona_name,
        pr_url=result.pr_url,
    )


def _infer_comment_category(body: str) -> str:
    """Infer category from inline comment body text for severity mapping.

    Simple keyword heuristic — not meant to be perfect, just reasonable.
    """
    body_lower = body.lower()
    if any(kw in body_lower for kw in ("security", "injection", "xss", "csrf", "auth")):
        return "Security"
    if any(kw in body_lower for kw in ("bug", "error", "crash", "null", "undefined", "exception")):
        return "Bugs"
    if any(kw in body_lower for kw in ("test", "coverage", "assert")):
        return "Testing"
    if any(kw in body_lower for kw in ("performance", "slow", "n+1", "cache", "memory")):
        return "Performance"
    if any(kw in body_lower for kw in ("architecture", "coupling", "dependency", "abstraction")):
        return "Architecture"
    return "Style"  # Default to lowest severity


def _create_lgtm_result(
    original: ReviewResult,
    min_severity: int,
) -> ReviewResult:
    """Create an LGTM result when all comments are filtered out.

    Posts a brief note that the review found only low-severity issues
    that were filtered per repo configuration.
    """
    return ReviewResult(
        verdict="approve",
        summary_sections=[
            CategorySection(
                emoji="✅",
                title="Filtered Review",
                findings=[
                    Finding(
                        text=(
                            f"All findings were below severity threshold "
                            f"(min_severity={min_severity}). No issues to report. "
                            f"Looks good! 🎉"
                        ),
                        confidence="high",
                    )
                ],
            )
        ],
        inline_comments=[],
        persona_name=original.persona_name,
        pr_url=original.pr_url,
    )


def _recompute_verdict(
    original_verdict: str,
    sections: list[CategorySection],
    inline_comments: Sequence[InlineComment],
) -> str:
    """Recompute verdict after filtering.

    Rules:
    - Never UPGRADES severity: if original was 'approve', stays 'approve'.
      If original was 'comment', can stay 'comment' or downgrade to 'approve',
      but never upgrade to 'request_changes'.
    - Only DOWNGRADES: 'request_changes' → 'comment' or 'approve' if blocking
      issues were filtered out.

    Verdict priority order (high → low): request_changes > comment > approve
    """
    # Never upgrade from the original verdict
    if original_verdict == "approve":
        return "approve"

    if not sections and not inline_comments:
        return "approve"

    # Check if any remaining sections are high-severity (blocking)
    has_blocking = any(
        CATEGORY_SEVERITY.get(s.title, 2) >= 3
        for s in sections
    )

    if has_blocking:
        # Only return request_changes if original was also request_changes
        if original_verdict == "request_changes":
            return "request_changes"
        return "comment"

    # Non-blocking findings remain — verdict is 'comment' at most
    if original_verdict in ("request_changes", "comment"):
        return "comment"

    return original_verdict
```

### 5.3 Orchestrator Integration

#### File: `review_bot/review/orchestrator.py` (modifications)

```python
from review_bot.review.severity import filter_result_by_severity

class ReviewOrchestrator:
    def __init__(
        self,
        github_client: GitHubAPIClient,
        persona_store: PersonaStore,
        db_engine: AsyncEngine | None = None,
        min_severity: int = 0,  # New parameter
    ) -> None:
        # ... existing init ...
        self._min_severity = min_severity

    async def run_review(self, ...) -> ReviewResult:
        # ... existing steps 1-6 ...

        # 6b. Apply severity filter (between formatter and poster)
        if self._min_severity > 0:
            result = filter_result_by_severity(result, self._min_severity)
            logger.info(
                "After severity filter (min=%d): sections=%d, comments=%d",
                self._min_severity,
                len(result.summary_sections),
                len(result.inline_comments),
            )

        # 7. Post review to GitHub
        # ... existing posting code ...
```

The `min_severity` value flows from:
1. `Settings.min_severity` (global default)
2. Overridden by `.review-like-him.yml` `min_severity` if present in the repo
3. Passed to `ReviewOrchestrator.__init__()` by the job queue worker

### 5.4 Security Override Mechanism

Critical security findings should bypass the severity filter regardless of threshold:

```python
# In severity.py:
SECURITY_OVERRIDE_KEYWORDS: list[str] = [
    "sql injection",
    "remote code execution",
    "rce",
    "path traversal",
    "command injection",
    "deserialization",
    "xxe",
    "ssrf",
    "hardcoded secret",
    "hardcoded password",
    "private key",
    "credential",
]


def _is_critical_security(body: str) -> bool:
    """Check if a finding describes a critical security issue that should bypass filters."""
    body_lower = body.lower()
    return any(kw in body_lower for kw in SECURITY_OVERRIDE_KEYWORDS)
```

This is checked in `filter_result_by_severity()` before filtering — any finding matching critical security keywords is always retained.

### 5.5 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| **All comments filtered out** | `_create_lgtm_result()` posts a "Filtered Review" section noting that all findings were below threshold. Does NOT post an empty review. |
| **Severity mapping inconsistencies** | `_infer_comment_category()` uses keyword heuristics for inline comments. If the LLM already provides a category in the comment body, that's preferred. Misclassification is acceptable — severity filtering is approximate. |
| **Override for critical security issues** | `_is_critical_security()` checks against a keyword list. These findings always pass the filter regardless of `min_severity`. |
| **Verdict changes after filtering** | `_recompute_verdict()` downgrades `request_changes` to `comment` or `approve` if the blocking issues were filtered. Never upgrades — if original was `approve`, stays `approve`. |
| **Per-repo config conflicts with global** | Per-repo `.review-like-him.yml` takes precedence over `Settings.min_severity`. If repo config says `min_severity: 0` and global says `3`, use `0`. |

### 5.6 Testing Approach

#### File: `tests/test_severity.py` (new)

- `test_compute_severity_security_high_confidence` — Security + high → 5 clamped to 4.
- `test_compute_severity_style_low_confidence` — Style + low → 0.
- `test_filter_removes_low_severity` — `min_severity=3` removes Style and Testing findings.
- `test_filter_keeps_all_at_zero` — `min_severity=0` returns result unchanged.
- `test_filter_all_removed_creates_lgtm` — All findings below threshold → LGTM result.
- `test_filter_recomputes_verdict_downgrade` — Blocking bugs filtered → verdict downgrades from `request_changes` to `comment`.
- `test_filter_recomputes_verdict_never_upgrades` — Original `approve` stays `approve` even if non-blocking sections remain.
- `test_security_override_bypasses_filter` — "SQL injection" finding kept even at `min_severity=4`.
- `test_infer_category_keywords` — Body with "null pointer" → `"Bugs"`.
- `test_inline_comment_category_inference` — Inline comment categorized for severity check.

---

## 6. File-type-aware review strategies

**Complexity:** 🟡 Medium
**Goal:** Inject file-type-specific review instructions into the LLM prompt so different file types get appropriate scrutiny.

### 6.1 File Type Classification

#### New file: `review_bot/review/file_strategy.py`

```python
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
    """Review strategy instructions for a specific file type."""

    file_type: str
    display_name: str
    review_focus: list[str]  # Key areas to focus on
    prompt_instructions: str  # Full text injected into LLM prompt
    severity_boost: int = 0  # Extra severity for findings in this file type


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
    )):
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
    """Get the review strategy for a file type."""
    return STRATEGIES.get(file_type)


def get_file_strategies(
    files: list[PullRequestFile],
) -> dict[str, list[PullRequestFile]]:
    """Group PR files by their file type classification.

    Returns a dict of FileType → list of files.
    """
    groups: dict[str, list[PullRequestFile]] = {}
    for f in files:
        ft = classify_file(f.filename)
        if ft == FileType.GENERATED:
            continue  # Skip generated files entirely
        groups.setdefault(ft, []).append(f)
    return groups
```

### 6.2 PromptBuilder Extension

#### File: `review_bot/review/prompt_builder.py` (modifications)

```python
from review_bot.review.file_strategy import (
    FileType,
    classify_file,
    get_strategy,
    get_file_strategies,
)

class PromptBuilder:
    def build(self, ...) -> str:
        # ... existing build logic ...

        # After building repo_context_text, add file-type strategy section
        file_strategy_text = self._format_file_strategies(files)

        # Insert file_strategy_text into the prompt template
        # (added as a new section before the Diff section)

    def _format_file_strategies(
        self,
        files: list[PullRequestFile],
    ) -> str:
        """Generate file-type-specific review instructions based on changed files.

        Only includes strategies for file types actually present in the PR.
        """
        file_groups = get_file_strategies(files)

        if not file_groups:
            return ""

        lines = ["## File-Type-Specific Instructions\n"]
        lines.append(
            "Apply these additional review rules based on the file types in this PR:\n"
        )

        for file_type, type_files in file_groups.items():
            strategy = get_strategy(file_type)
            if strategy is None:
                continue

            file_list = ", ".join(f"`{f.filename}`" for f in type_files[:5])
            if len(type_files) > 5:
                file_list += f", ... ({len(type_files) - 5} more)"

            lines.append(f"### {strategy.display_name} ({file_list})\n")
            lines.append(strategy.prompt_instructions)
            lines.append("")

        return "\n".join(lines)
```

### 6.3 Updated Prompt Template

Add a `{file_strategy_text}` placeholder to `SYSTEM_PROMPT_TEMPLATE`:

```python
SYSTEM_PROMPT_TEMPLATE = """\
You are {persona_name}-bot 🤖, ...

## Your Persona
...

## Repository Context
{repo_context_text}\

{file_strategy_text}\

## Instructions
...
"""
```

### 6.4 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| **Polyglot files** | Files are classified by their primary extension. A `.py` file with embedded SQL is classified as `business_logic`, but SQL keywords in migration directories still trigger `migration` classification. |
| **Generated files** | `classify_file()` detects generated files and `get_file_strategies()` excludes them entirely. Generated files get no review instructions. |
| **Config files that look like code** | `setup.py` is classified as `config` (it's in the config name list). Complex build scripts that happen to be `.py` files in the root are also classified as config — this is a reasonable trade-off. |
| **File type detection failures** | Unknown file types get `FileType.UNKNOWN` and no strategy instructions. The LLM still reviews them using the persona's general priorities. |
| **Custom file extensions** | Unrecognized extensions fall through to `UNKNOWN`. Users can't add custom extensions without code changes (a future plugin system, Phase 4, would address this). |
| **PR with many file types** | Strategy text for all present file types is included. Capped implicitly by prompt size — if the PR has too many file types, less important ones (DOCUMENTATION, CONFIG) are truncated first. |
| **Severity boost from file type** | `FileTypeStrategy.severity_boost` is applied in the severity filtering module (item 5). Migration files get +1 boost, documentation gets -1. This is additive with category severity and confidence modifiers. |

### 6.5 Integration with Severity Filtering (Item 5)

The `severity_boost` from file-type strategies is applied when computing finding severity:

```python
# In severity.py, update compute_finding_severity:
def compute_finding_severity(
    category: str,
    confidence: str = "medium",
    file_type: str = "unknown",
) -> int:
    """Compute effective severity with file-type boost."""
    base = CATEGORY_SEVERITY.get(category, 2)
    confidence_mod = CONFIDENCE_SEVERITY_BOOST.get(confidence, 0)
    file_type_mod = STRATEGIES.get(file_type, FileTypeStrategy(...)).severity_boost
    return max(0, min(4, base + confidence_mod + file_type_mod))
```

### 6.6 Testing Approach

#### File: `tests/test_file_strategy.py` (new)

- `test_classify_python_migration` — `migrations/0001_initial.py` → `FileType.MIGRATION`.
- `test_classify_sql_migration` — `db/migrate/202603151200_add_users.sql` → `FileType.MIGRATION`.
- `test_classify_alembic_migration` — `alembic/versions/abc123.py` → `FileType.MIGRATION`.
- `test_classify_test_file` — `tests/test_api.py` → `FileType.TEST`.
- `test_classify_spec_file` — `src/components/Button.spec.tsx` → `FileType.TEST`.
- `test_classify_generated_file` — `dist/bundle.min.js` → `FileType.GENERATED`.
- `test_classify_api_route` — `src/api/routes/users.py` → `FileType.API_DEFINITION`.
- `test_classify_dockerfile` — `Dockerfile` → `FileType.INFRASTRUCTURE`.
- `test_classify_config` — `pyproject.toml` → `FileType.CONFIG`.
- `test_classify_markdown` — `docs/README.md` → `FileType.DOCUMENTATION`.
- `test_classify_business_logic` — `src/services/payment.py` → `FileType.BUSINESS_LOGIC`.
- `test_classify_unknown_extension` — `data/file.xyz` → `FileType.UNKNOWN`.
- `test_get_file_strategies_groups_correctly` — Mixed file list grouped by type.
- `test_get_file_strategies_excludes_generated` — Generated files not in any group.
- `test_format_file_strategies_output` — Strategy text includes correct instructions.
- `test_format_file_strategies_caps_file_list` — >5 files shows "... (N more)".
- `test_migration_strategy_instructions` — Migration strategy includes DROP/TRUNCATE rules.
- `test_severity_boost_applied` — Migration finding severity boosted by 1.

---

## Cross-cutting Concerns

### Dependency Order

The 6 items have the following dependencies:

```
Item 4 (confidence) ← Item 5 (severity filtering) uses confidence for severity modifiers
Item 4 (confidence) ← Item 6 (file strategies) findings include confidence
Item 2 (context) ← Item 1 (multi-pass) chunk context uses module boundaries
Item 4 (confidence) ← Item 3 (feedback) tracks confidence accuracy
```

**Recommended implementation order:**

1. **Item 4** — Confidence scores (foundation for severity and feedback)
2. **Item 6** — File-type strategies (standalone, small scope)
3. **Item 5** — Severity filtering (depends on confidence from 4, file types from 6)
4. **Item 2** — Context-aware reviews (standalone but large)
5. **Item 1** — Multi-pass review (benefits from context awareness)
6. **Item 3** — Learning from feedback (requires all other items to be generating reviewable output)

### Database Migration Requirement

Items 3 (feedback tables) and potentially 5 (per-repo settings cache) require new database tables. The roadmap's Technical Debt section notes that Alembic migrations should replace raw `CREATE TABLE IF NOT EXISTS`. Ideally, implement the Alembic migration framework before adding new tables, but if not, use the existing `CREATE TABLE IF NOT EXISTS` pattern in `create_app()` for consistency.

### Prompt Size Budget

Current `MAX_DIFF_CHARS = 80_000`. With the additions from these items:

| Section | Estimated chars |
|---------|----------------|
| Persona (existing) | ~800 |
| Repo context (existing) | ~300 |
| Repo context (new: modules, APIs) | ~1,500 |
| Confidence instructions | ~400 |
| File-type strategies | ~1,200 |
| Cross-chunk context (multi-pass) | ~500 |
| Instructions (existing) | ~600 |
| **Total non-diff overhead** | **~5,300** |

This leaves ~74,700 chars for diff content per prompt, which is acceptable. The chunker's `DEFAULT_CHUNK_MAX_CHARS = 70,000` accounts for this overhead.

### Backward Compatibility

All changes maintain backward compatibility:

- `CategorySection.findings` accepts both `list[str]` (old) and `list[Finding]` (new)
- `InlineComment` new fields (`confidence`, `confidence_reason`) have defaults
- `RepoContext` new fields all have defaults (existing code sees empty lists/strings)
- `Settings.min_severity` defaults to 0 (no filtering, same as current behavior)
- `LARGE_PR_FILE_THRESHOLD` constant kept at 500 (behavior changes but constant name preserved)
- `ReviewOrchestrator.__init__()` new parameters have defaults

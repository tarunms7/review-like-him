# Phase 4 — Platform: Implementation Plan

> Last updated: 2026-03-15

This document provides exhaustive implementation details for all 6 Phase 4 roadmap items. Phase 4 expands review-like-him beyond GitHub and Claude to become a general-purpose review platform — supporting multiple code hosting providers, LLM backends, plugin extensibility, and production deployment patterns.

---

## Table of Contents

1. [GitLab / Bitbucket Support](#1-gitlab--bitbucket-support)
2. [Custom LLM Backends](#2-custom-llm-backends)
3. [Persona Marketplace](#3-persona-marketplace)
4. [Programmatic API](#4-programmatic-api)
5. [Plugin System for Custom Review Rules](#5-plugin-system-for-custom-review-rules)
6. [Self-Hosted Deployment Guide](#6-self-hosted-deployment-guide)

---

## 1. GitLab / Bitbucket Support

### Overview

Abstract the GitHub-specific code (`github/api.py`, `github/app.py`, `github/setup.py`) behind a platform interface, then implement GitLab and Bitbucket backends. The mining, review, and posting logic remains the same — only the API layer changes. GitLab uses project access tokens and a different webhook payload format; Bitbucket uses app passwords and its own REST API v2.0.

### Current State

All platform interaction is hardcoded to GitHub:

- `GitHubAPIClient` in `review_bot/github/api.py` uses `https://api.github.com` directly with GitHub-specific endpoints (`/repos/{owner}/{repo}/pulls/{pr}`, `/repos/{owner}/{repo}/pulls/{pr}/reviews`)
- `GitHubAppAuth` in `review_bot/github/app.py` implements GitHub App JWT flow with RS256 signing and installation token caching
- `review_bot/server/webhooks.py` parses GitHub-specific webhook payloads (`pull_request`, `issue_comment` events) with `X-Hub-Signature-256` HMAC validation
- `ReviewOrchestrator` in `review_bot/review/orchestrator.py` calls `GitHubAPIClient` methods directly and constructs `github.com` URLs
- Data classes `PullRequestFile` and `ReviewComment` are defined in `review_bot/github/api.py` with GitHub-specific field names

### Implementation Steps

#### 1. Define platform protocol interfaces

**`review_bot/platform/__init__.py`**

New package for platform abstraction.

**`review_bot/platform/base.py`**

Define `Protocol` classes that all platform backends must implement:

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class PlatformAuth(Protocol):
    """Authentication for a code hosting platform."""

    async def get_authenticated_client(self, installation_id: int | str) -> httpx.AsyncClient:
        """Return an HTTP client with valid auth headers."""
        ...

@runtime_checkable
class PlatformAPI(Protocol):
    """Code hosting platform API for PR operations."""

    async def get_merge_request(self, project: str, mr_id: int) -> MergeRequestData:
        ...

    async def get_merge_request_diff(self, project: str, mr_id: int) -> str:
        ...

    async def get_merge_request_files(self, project: str, mr_id: int) -> list[ChangedFile]:
        ...

    async def post_review(
        self, project: str, mr_id: int, body: str, verdict: str,
        comments: list[InlineComment] | None = None,
    ) -> dict:
        ...

    async def post_comment(self, project: str, mr_id: int, body: str) -> dict:
        ...

    async def get_user_reviews(self, username: str, **kwargs) -> list[dict]:
        ...

    async def get_file_contents(self, project: str, path: str) -> dict:
        ...

@runtime_checkable
class WebhookHandler(Protocol):
    """Parses platform-specific webhook payloads."""

    def verify_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        ...

    def parse_event(self, headers: dict[str, str], payload: dict) -> WebhookEvent | None:
        ...
```

**`review_bot/platform/models.py`**

Platform-agnostic data models replacing `PullRequestFile` and `ReviewComment`:

```python
@dataclass
class ChangedFile:
    filename: str
    status: str  # "added" | "modified" | "removed"
    additions: int
    deletions: int
    patch: str | None = None

@dataclass
class InlineComment:
    path: str
    line: int
    body: str

@dataclass
class MergeRequestData:
    id: int
    title: str
    author: str
    description: str
    url: str
    additions: int
    deletions: int
    changed_files: int
    raw: dict  # Original platform-specific payload

@dataclass
class WebhookEvent:
    event_type: str  # "review_requested" | "comment_command" | "label"
    project: str  # "owner/repo" for GitHub, project ID for GitLab
    mr_id: int
    persona_name: str | None
    installation_id: int | str
    platform: str  # "github" | "gitlab" | "bitbucket"
```

#### 2. Refactor GitHub backend to implement protocols

**`review_bot/platform/github/__init__.py`**

Move existing `github/` code under `platform/github/` and make it conform to the protocol interfaces.

**`review_bot/platform/github/api.py`**

Wrap existing `GitHubAPIClient` to implement `PlatformAPI`:

```python
class GitHubPlatformAPI:
    """GitHub implementation of PlatformAPI protocol."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._client = GitHubAPIClient(http_client)

    async def get_merge_request(self, project: str, mr_id: int) -> MergeRequestData:
        owner, repo = project.split("/", 1)
        raw = await self._client.get_pull_request(owner, repo, mr_id)
        return MergeRequestData(
            id=mr_id,
            title=raw.get("title", ""),
            author=raw.get("user", {}).get("login", "unknown"),
            description=raw.get("body", "") or "",
            url=raw.get("html_url", ""),
            additions=raw.get("additions", 0),
            deletions=raw.get("deletions", 0),
            changed_files=raw.get("changed_files", 0),
            raw=raw,
        )
```

**`review_bot/platform/github/webhooks.py`**

Extract GitHub-specific webhook parsing from `server/webhooks.py` into a `GitHubWebhookHandler` implementing `WebhookHandler`.

#### 3. Implement GitLab backend

**`review_bot/platform/gitlab/__init__.py`**
**`review_bot/platform/gitlab/api.py`**

```python
GITLAB_API_BASE = "https://gitlab.com/api/v4"

class GitLabPlatformAPI:
    """GitLab implementation of PlatformAPI protocol."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._client = http_client

    async def get_merge_request(self, project: str, mr_id: int) -> MergeRequestData:
        project_encoded = quote(project, safe="")
        resp = await self._client.get(
            f"{GITLAB_API_BASE}/projects/{project_encoded}/merge_requests/{mr_id}"
        )
        raw = resp.json()
        return MergeRequestData(
            id=mr_id,
            title=raw.get("title", ""),
            author=raw.get("author", {}).get("username", "unknown"),
            description=raw.get("description", "") or "",
            url=raw.get("web_url", ""),
            additions=raw.get("changes_count", 0),
            deletions=0,
            changed_files=raw.get("changes_count", 0),
            raw=raw,
        )

    async def post_review(
        self, project: str, mr_id: int, body: str, verdict: str,
        comments: list[InlineComment] | None = None,
    ) -> dict:
        project_encoded = quote(project, safe="")
        # GitLab uses notes for MR comments
        resp = await self._client.post(
            f"{GITLAB_API_BASE}/projects/{project_encoded}/merge_requests/{mr_id}/notes",
            json={"body": body},
        )
        # Post inline discussions for line-level comments
        if comments:
            for c in comments:
                await self._post_inline_discussion(project_encoded, mr_id, c)
        return resp.json()
```

**`review_bot/platform/gitlab/auth.py`**

```python
class GitLabAuth:
    """GitLab authentication via project/group access tokens."""

    def __init__(self, access_token: str, base_url: str = "https://gitlab.com") -> None:
        self._token = access_token
        self._base_url = base_url

    async def get_authenticated_client(self, installation_id: int | str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"PRIVATE-TOKEN": self._token},
            base_url=f"{self._base_url}/api/v4",
        )
```

**`review_bot/platform/gitlab/webhooks.py`**

GitLab webhook handler parsing `X-Gitlab-Token` header and `merge_request` / `note` events.

#### 4. Implement Bitbucket backend

**`review_bot/platform/bitbucket/__init__.py`**
**`review_bot/platform/bitbucket/api.py`**

Bitbucket Cloud REST API v2.0 implementation. Key differences:
- PRs are at `/2.0/repositories/{workspace}/{repo}/pullrequests/{id}`
- Inline comments use `/2.0/repositories/{workspace}/{repo}/pullrequests/{id}/comments`
- Diff endpoint returns raw diff via `Accept: text/plain`

**`review_bot/platform/bitbucket/auth.py`**

Bitbucket uses app passwords or OAuth2 consumer credentials.

#### 5. Add platform factory and update orchestrator

**`review_bot/platform/factory.py`**

```python
def create_platform(
    platform: str,
    settings: Settings,
) -> tuple[PlatformAuth, PlatformAPI, WebhookHandler]:
    """Factory function to create platform-specific components."""
    if platform == "github":
        ...
    elif platform == "gitlab":
        ...
    elif platform == "bitbucket":
        ...
    else:
        raise ValueError(f"Unsupported platform: {platform}")
```

**`review_bot/review/orchestrator.py`**

Replace `GitHubAPIClient` dependency with `PlatformAPI`:

```python
class ReviewOrchestrator:
    def __init__(
        self,
        platform_api: PlatformAPI,  # was: github_client: GitHubAPIClient
        persona_store: PersonaStore,
        db_engine: AsyncEngine | None = None,
    ) -> None:
```

**`review_bot/server/webhooks.py`**

Replace hardcoded GitHub webhook parsing with delegating to `WebhookHandler`:

```python
def configure(
    job_queue: AsyncJobQueue,
    webhook_handler: WebhookHandler,  # NEW: replaces webhook_secret
    persona_store,
) -> None:
```

#### 6. Update CLI and server startup

**`review_bot/config/settings.py`**

Add platform configuration:

```python
class Settings(BaseSettings):
    platform: str = Field(default="github", description="Code hosting platform: github, gitlab, bitbucket")
    gitlab_url: str = Field(default="https://gitlab.com", description="GitLab instance URL")
    gitlab_token: str = Field(default="", description="GitLab access token")
    bitbucket_username: str = Field(default="", description="Bitbucket username")
    bitbucket_app_password: str = Field(default="", description="Bitbucket app password")
```

**`review_bot/server/app.py`**

Use `create_platform()` factory based on `settings.platform`.

**`review_bot/cli/review_cmd.py`**

Add `--platform` flag to `review` command.

### Database Changes

None — the reviews table stores `repo` as `owner/repo` text and `pr_url` as a full URL. Both are platform-agnostic.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `REVIEW_BOT_PLATFORM` | `github` | Code hosting platform |
| `REVIEW_BOT_GITLAB_URL` | `https://gitlab.com` | GitLab instance base URL |
| `REVIEW_BOT_GITLAB_TOKEN` | `""` | GitLab project/group access token |
| `REVIEW_BOT_BITBUCKET_USERNAME` | `""` | Bitbucket Cloud username |
| `REVIEW_BOT_BITBUCKET_APP_PASSWORD` | `""` | Bitbucket app password |

CLI flag: `--platform github|gitlab|bitbucket` on `review-bot review` and `review-bot serve`.

### Testing Strategy

- Unit tests for each platform API class using `respx` to mock HTTP responses
- Fixture files with sample webhook payloads for each platform (GitHub, GitLab, Bitbucket)
- Integration test verifying the `PlatformAPI` protocol compliance via `isinstance` checks
- Test the factory function with each platform string
- End-to-end test: mock GitLab API → orchestrator → formatted review output

### Estimated Effort

🔴 Large (5–7 days)

- 2 days: protocol design + GitHub refactor
- 2 days: GitLab backend + webhook handler
- 1–2 days: Bitbucket backend
- 1 day: CLI/settings/factory wiring + tests

---

## 2. Custom LLM Backends

### Overview

Support OpenAI, Anthropic direct API, and local models (Ollama, vLLM) as alternatives to Claude Agent SDK. The `ClaudeReviewer` class in `review_bot/review/reviewer.py` is the only LLM touchpoint — wrap it in a `ReviewerBackend` protocol with implementations for each provider.

### Current State

- `ClaudeReviewer` in `review_bot/review/reviewer.py` imports `claude_agent_sdk` directly and calls `query()` with `ClaudeAgentOptions(max_turns=1)`
- The reviewer collects `AssistantMessage` content blocks with `.text` attributes
- `PersonaAnalyzer` in `review_bot/persona/analyzer.py` also uses `claude_agent_sdk` directly with the same pattern
- No configuration exists for LLM provider selection — `Settings` has no LLM-related fields
- Retry logic (exponential backoff on 429s) is baked into `ClaudeReviewer`

### Implementation Steps

#### 1. Define reviewer backend protocol

**`review_bot/review/backends/__init__.py`**

```python
from review_bot.review.backends.base import ReviewerBackend
from review_bot.review.backends.factory import create_reviewer_backend
```

**`review_bot/review/backends/base.py`**

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class ReviewerBackend(Protocol):
    """Protocol for LLM backends that execute review prompts."""

    async def generate(self, prompt: str) -> str:
        """Send a prompt to the LLM and return the raw text response.

        Implementations must handle their own retry logic and rate limiting.

        Args:
            prompt: The full review prompt.

        Returns:
            Raw text output from the LLM.

        Raises:
            RuntimeError: If generation fails after all retry attempts.
        """
        ...

    @property
    def model_name(self) -> str:
        """Return the model identifier for logging."""
        ...
```

#### 2. Extract Claude Agent SDK into a backend

**`review_bot/review/backends/claude_sdk.py`**

Refactor existing `ClaudeReviewer` logic:

```python
class ClaudeSDKBackend:
    """Claude Agent SDK backend (existing behavior)."""

    def __init__(self, max_retries: int = 3) -> None:
        self._max_retries = max_retries

    @property
    def model_name(self) -> str:
        return "claude-agent-sdk"

    async def generate(self, prompt: str) -> str:
        # Move existing ClaudeReviewer.review() logic here
        ...
```

#### 3. Implement OpenAI backend

**`review_bot/review/backends/openai.py`**

```python
class OpenAIBackend:
    """OpenAI API backend using httpx (no SDK dependency)."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str = "https://api.openai.com/v1",
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._max_retries = max_retries

    @property
    def model_name(self) -> str:
        return f"openai/{self._model}"

    async def generate(self, prompt: str) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                },
                timeout=120.0,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
```

#### 4. Implement Anthropic direct API backend

**`review_bot/review/backends/anthropic.py`**

```python
class AnthropicBackend:
    """Anthropic Messages API backend (direct, no SDK wrapper)."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        max_retries: int = 3,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_retries = max_retries

    @property
    def model_name(self) -> str:
        return f"anthropic/{self._model}"

    async def generate(self, prompt: str) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self._model,
                    "max_tokens": 8192,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=120.0,
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
```

#### 5. Implement local model backend (Ollama / vLLM)

**`review_bot/review/backends/local.py`**

```python
class LocalModelBackend:
    """Backend for local models via OpenAI-compatible API (Ollama, vLLM, etc.)."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        model: str = "llama3",
        max_retries: int = 2,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_retries = max_retries

    @property
    def model_name(self) -> str:
        return f"local/{self._model}"

    async def generate(self, prompt: str) -> str:
        # Uses OpenAI-compatible /chat/completions endpoint
        ...
```

#### 6. Add backend factory and update settings

**`review_bot/review/backends/factory.py`**

```python
def create_reviewer_backend(settings: Settings) -> ReviewerBackend:
    """Create a reviewer backend based on configuration."""
    backend = settings.llm_backend
    if backend == "claude-sdk":
        from review_bot.review.backends.claude_sdk import ClaudeSDKBackend
        return ClaudeSDKBackend(max_retries=settings.llm_max_retries)
    elif backend == "openai":
        from review_bot.review.backends.openai import OpenAIBackend
        return OpenAIBackend(
            api_key=settings.openai_api_key,
            model=settings.llm_model or "gpt-4o",
            max_retries=settings.llm_max_retries,
        )
    elif backend == "anthropic":
        from review_bot.review.backends.anthropic import AnthropicBackend
        return AnthropicBackend(
            api_key=settings.anthropic_api_key,
            model=settings.llm_model or "claude-sonnet-4-20250514",
            max_retries=settings.llm_max_retries,
        )
    elif backend == "local":
        from review_bot.review.backends.local import LocalModelBackend
        return LocalModelBackend(
            base_url=settings.llm_base_url or "http://localhost:11434/v1",
            model=settings.llm_model or "llama3",
            max_retries=settings.llm_max_retries,
        )
    else:
        raise ValueError(f"Unknown LLM backend: {backend}")
```

#### 7. Update orchestrator and analyzer

**`review_bot/review/orchestrator.py`**

Replace `ClaudeReviewer` with `ReviewerBackend`:

```python
class ReviewOrchestrator:
    def __init__(
        self,
        github_client: GitHubAPIClient,
        persona_store: PersonaStore,
        db_engine: AsyncEngine | None = None,
        reviewer_backend: ReviewerBackend | None = None,
    ) -> None:
        self._reviewer = reviewer_backend or ClaudeSDKBackend()
```

Change `self._reviewer.review(prompt)` calls to `self._reviewer.generate(prompt)`.

**`review_bot/persona/analyzer.py`**

Replace direct `claude_agent_sdk` usage with `ReviewerBackend`:

```python
class PersonaAnalyzer:
    def __init__(self, backend: ReviewerBackend | None = None) -> None:
        self._backend = backend or ClaudeSDKBackend()

    async def analyze(self, ...) -> PersonaProfile:
        result_text = await self._backend.generate(prompt)
```

### Database Changes

Add `llm_backend` column to the `reviews` table for tracking which model produced each review:

```sql
ALTER TABLE reviews ADD COLUMN llm_backend TEXT NOT NULL DEFAULT 'claude-sdk';
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `REVIEW_BOT_LLM_BACKEND` | `claude-sdk` | LLM backend: `claude-sdk`, `openai`, `anthropic`, `local` |
| `REVIEW_BOT_LLM_MODEL` | `""` | Model override (e.g., `gpt-4o`, `claude-sonnet-4-20250514`, `llama3`) |
| `REVIEW_BOT_LLM_BASE_URL` | `""` | Base URL for local model backends |
| `REVIEW_BOT_LLM_MAX_RETRIES` | `3` | Maximum retry attempts for LLM calls |
| `REVIEW_BOT_OPENAI_API_KEY` | `""` | OpenAI API key |
| `REVIEW_BOT_ANTHROPIC_API_KEY` | `""` | Anthropic API key |

CLI flags: `--llm-backend`, `--llm-model` on `review-bot review` and `review-bot persona mine`.

### Testing Strategy

- Unit test each backend with mocked HTTP responses using `respx`
- Protocol compliance test: `assert isinstance(backend, ReviewerBackend)` for all implementations
- Integration test: factory creates correct backend for each `llm_backend` setting
- Test retry logic independently per backend (mock 429 → success sequence)
- Test `PersonaAnalyzer` with a mock `ReviewerBackend` returning fixture JSON

### Estimated Effort

🟡 Medium (2–3 days)

- 0.5 days: protocol + factory + settings
- 0.5 days: extract Claude SDK backend
- 1 day: OpenAI + Anthropic + local backends
- 0.5 days: update orchestrator/analyzer + tests

---

## 3. Persona Marketplace

### Overview

A hosted registry where teams can publish anonymized review styles (e.g., "Security-focused senior engineer") for others to discover and use. Personas are stripped of identifying information and published as YAML profiles with versioning, search, and privacy guarantees.

### Current State

- `PersonaStore` in `review_bot/persona/store.py` manages personas as YAML files on disk in `~/.review-bot/personas/`
- Store operations are purely local: `save()`, `load()`, `list_all()`, `delete()`, `exists()`
- `PersonaProfile` includes `github_user` and `mined_from` fields that contain identifying information
- `PersonaAnalyzer` in `review_bot/persona/analyzer.py` produces profiles with `pet_peeves`, `tone`, and `priorities` — all derived from a specific user's reviews
- No network-based persona sharing exists

### Implementation Steps

#### 1. Design the marketplace API

**`review_bot/marketplace/__init__.py`**

New package for marketplace client and server components.

**`review_bot/marketplace/models.py`**

```python
from pydantic import BaseModel, Field

class MarketplacePersona(BaseModel):
    """A published persona in the marketplace."""
    slug: str = Field(description="URL-friendly unique identifier")
    display_name: str = Field(description="Human-readable name, e.g., 'Security-Focused Senior Engineer'")
    description: str = Field(description="What this reviewer cares about")
    version: int = Field(default=1, description="Monotonically increasing version")
    tags: list[str] = Field(default_factory=list, description="Searchable tags")
    download_count: int = Field(default=0)
    profile_yaml: str = Field(description="Anonymized persona YAML")
    published_at: str = Field(description="ISO 8601 timestamp")
    updated_at: str = Field(description="ISO 8601 timestamp")

class PublishRequest(BaseModel):
    display_name: str
    description: str
    tags: list[str] = []

class SearchParams(BaseModel):
    query: str = ""
    tags: list[str] = []
    sort_by: str = "download_count"  # "download_count" | "updated_at" | "relevance"
    page: int = 1
    per_page: int = 20
```

#### 2. Build the anonymization pipeline

**`review_bot/marketplace/anonymizer.py`**

Strip identifying information from a `PersonaProfile` before publishing:

```python
class PersonaAnonymizer:
    """Strips identifying information from persona profiles."""

    # Patterns that might reveal identity
    _USERNAME_FIELDS = {"github_user", "mined_from", "name"}
    _REDACT_PATTERNS = [
        re.compile(r"@[\w-]+"),  # @mentions
        re.compile(r"github\.com/[\w-]+"),  # GitHub profile URLs
        re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b"),  # Proper names (heuristic)
    ]

    def anonymize(self, profile: PersonaProfile, display_name: str) -> str:
        """Return anonymized YAML string.

        Steps:
        1. Remove github_user, mined_from, name fields
        2. Replace any @mentions in tone/pet_peeves with [redacted]
        3. Strip repo-specific references from priorities
        4. Set name to a generated slug
        """
        data = profile.model_dump()
        data.pop("github_user", None)
        data.pop("mined_from", None)
        data.pop("last_mined_at", None)
        data["name"] = self._slugify(display_name)

        # Redact text fields
        for field in ("tone", "pet_peeves", "overrides"):
            if field in data:
                data[field] = self._redact_field(data[field])

        return yaml.dump(data, default_flow_style=False, sort_keys=False)

    def _redact_field(self, value: str | list[str]) -> str | list[str]:
        """Replace identifying patterns with [redacted]."""
        if isinstance(value, list):
            return [self._redact_text(v) for v in value]
        return self._redact_text(value)

    def _redact_text(self, text: str) -> str:
        for pattern in self._REDACT_PATTERNS:
            text = pattern.sub("[redacted]", text)
        return text

    def _slugify(self, name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
```

#### 3. Implement marketplace client

**`review_bot/marketplace/client.py`**

```python
class MarketplaceClient:
    """HTTP client for the persona marketplace registry."""

    def __init__(
        self,
        registry_url: str = "https://marketplace.review-like-him.dev/api/v1",
        api_key: str | None = None,
    ) -> None:
        self._base_url = registry_url.rstrip("/")
        self._api_key = api_key

    async def search(self, params: SearchParams) -> list[MarketplacePersona]:
        """Search the marketplace for personas."""
        ...

    async def publish(
        self, profile: PersonaProfile, request: PublishRequest,
    ) -> MarketplacePersona:
        """Publish an anonymized persona to the marketplace."""
        ...

    async def download(self, slug: str) -> PersonaProfile:
        """Download a persona and return a usable PersonaProfile."""
        ...

    async def list_versions(self, slug: str) -> list[MarketplacePersona]:
        """List all versions of a published persona."""
        ...
```

#### 4. Add marketplace server endpoints

**`review_bot/marketplace/server.py`**

FastAPI router for hosting a marketplace registry (for self-hosted deployments):

```python
router = APIRouter(prefix="/api/v1/marketplace", tags=["marketplace"])

@router.get("/personas")
async def search_personas(query: str = "", tags: str = "", sort_by: str = "download_count"):
    ...

@router.get("/personas/{slug}")
async def get_persona(slug: str):
    ...

@router.post("/personas")
async def publish_persona(request: PublishRequest, api_key: str = Header(...)):
    ...

@router.get("/personas/{slug}/versions")
async def list_versions(slug: str):
    ...

@router.get("/personas/{slug}/download")
async def download_persona(slug: str):
    ...
```

#### 5. Add marketplace database tables

New tables for marketplace storage:

```sql
CREATE TABLE IF NOT EXISTS marketplace_personas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',  -- JSON array
    download_count INTEGER NOT NULL DEFAULT 0,
    published_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    publisher_api_key_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS marketplace_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_slug TEXT NOT NULL REFERENCES marketplace_personas(slug),
    version INTEGER NOT NULL,
    profile_yaml TEXT NOT NULL,
    published_at TEXT NOT NULL,
    UNIQUE(persona_slug, version)
);

CREATE TABLE IF NOT EXISTS marketplace_api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
```

#### 6. Add CLI commands

**`review_bot/cli/marketplace_cmd.py`**

```python
@click.group()
def marketplace():
    """Browse and publish personas on the marketplace."""

@marketplace.command()
@click.argument("query", required=False)
@click.option("--tags", "-t", multiple=True)
def search(query: str | None, tags: tuple[str, ...]):
    """Search the persona marketplace."""

@marketplace.command()
@click.argument("persona_name")
@click.option("--display-name", required=True)
@click.option("--description", required=True)
@click.option("--tags", "-t", multiple=True)
def publish(persona_name: str, display_name: str, description: str, tags: tuple[str, ...]):
    """Publish a persona to the marketplace (anonymized)."""

@marketplace.command()
@click.argument("slug")
@click.option("--name", help="Local persona name to save as")
def install(slug: str, name: str | None):
    """Download and install a persona from the marketplace."""
```

### Database Changes

Three new tables: `marketplace_personas`, `marketplace_versions`, `marketplace_api_keys` (see step 5 above).

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `REVIEW_BOT_MARKETPLACE_URL` | `https://marketplace.review-like-him.dev/api/v1` | Marketplace registry URL |
| `REVIEW_BOT_MARKETPLACE_API_KEY` | `""` | API key for publishing personas |

CLI flags: `--registry-url` on marketplace subcommands.

### Testing Strategy

- Unit test `PersonaAnonymizer` with fixtures containing @mentions, names, and GitHub URLs
- Verify no identifying information leaks through anonymization (property-based test)
- Mock marketplace API endpoints with `respx` for client tests
- Integration test: mine persona → anonymize → publish → download → verify profile structure
- Test search with various query/tag combinations
- Test version bumping on re-publish

### Estimated Effort

🔴 Large (5–7 days)

- 1 day: models + anonymizer
- 1 day: marketplace client
- 2 days: marketplace server + database
- 1 day: CLI commands
- 1 day: privacy review + testing

---

## 4. Programmatic API

### Overview

A documented REST API for triggering reviews, managing personas, and querying review history without the CLI or webhooks. Extends the existing FastAPI server with authenticated CRUD endpoints, API key management, and auto-generated OpenAPI documentation.

### Current State

- `review_bot/server/app.py` creates a FastAPI app with only the webhook router mounted
- The app has a `/webhook` POST endpoint but no REST API for external consumers
- No API authentication exists beyond webhook HMAC validation
- No `routes/` directory exists — all routing is in `webhooks.py`
- Database tables (`reviews`, `jobs`, `persona_stats`) already store data that the API would expose
- `PersonaStore` provides `save()`, `load()`, `list_all()`, `delete()`, `exists()` operations

### Implementation Steps

#### 1. Add API key authentication

**`review_bot/server/auth.py`**

```python
import hashlib
import secrets
from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

_api_key_header = APIKeyHeader(name="X-API-Key")

def generate_api_key() -> tuple[str, str]:
    """Generate a new API key and its hash.

    Returns:
        Tuple of (raw_key, key_hash).
    """
    raw_key = f"rlh_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    return raw_key, key_hash

async def verify_api_key(
    api_key: str = Security(_api_key_header),
) -> str:
    """FastAPI dependency that validates API keys against the database."""
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    # Query api_keys table for matching hash
    # Raise HTTPException(401) if not found
    return key_hash
```

#### 2. Create API route modules

**`review_bot/server/routes/__init__.py`**

**`review_bot/server/routes/reviews.py`**

```python
router = APIRouter(prefix="/api/v1/reviews", tags=["reviews"])

@router.post("/", status_code=202)
async def trigger_review(
    request: TriggerReviewRequest,
    _key: str = Depends(verify_api_key),
) -> TriggerReviewResponse:
    """Trigger a new review on a pull request.

    Queues the review job and returns immediately with a job ID.
    """
    ...

@router.get("/{review_id}")
async def get_review(
    review_id: int,
    _key: str = Depends(verify_api_key),
) -> ReviewResponse:
    """Get a specific review by ID."""
    ...

@router.get("/")
async def list_reviews(
    persona: str | None = None,
    repo: str | None = None,
    page: int = 1,
    per_page: int = 20,
    _key: str = Depends(verify_api_key),
) -> PaginatedResponse[ReviewResponse]:
    """List reviews with optional filtering."""
    ...
```

**`review_bot/server/routes/personas.py`**

```python
router = APIRouter(prefix="/api/v1/personas", tags=["personas"])

@router.get("/")
async def list_personas(_key: str = Depends(verify_api_key)) -> list[PersonaSummary]:
    """List all configured personas."""
    ...

@router.get("/{name}")
async def get_persona(name: str, _key: str = Depends(verify_api_key)) -> PersonaDetail:
    """Get full persona profile by name."""
    ...

@router.delete("/{name}", status_code=204)
async def delete_persona(name: str, _key: str = Depends(verify_api_key)) -> None:
    """Delete a persona."""
    ...

@router.post("/{name}/mine", status_code=202)
async def trigger_mine(
    name: str,
    request: MineRequest,
    _key: str = Depends(verify_api_key),
) -> MineResponse:
    """Trigger persona mining from a GitHub user."""
    ...
```

**`review_bot/server/routes/jobs.py`**

```python
router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

@router.get("/{job_id}")
async def get_job(job_id: str, _key: str = Depends(verify_api_key)) -> JobResponse:
    """Get job status and result."""
    ...

@router.get("/")
async def list_jobs(
    status: str | None = None,
    page: int = 1,
    per_page: int = 20,
    _key: str = Depends(verify_api_key),
) -> PaginatedResponse[JobResponse]:
    """List jobs with optional status filtering."""
    ...
```

#### 3. Define request/response models

**`review_bot/server/schemas.py`**

```python
from pydantic import BaseModel, Field

class TriggerReviewRequest(BaseModel):
    pr_url: str = Field(description="Full PR URL (e.g., https://github.com/owner/repo/pull/123)")
    persona_name: str = Field(description="Persona to review as")

class TriggerReviewResponse(BaseModel):
    job_id: str
    status: str = "queued"
    message: str = "Review queued successfully"

class ReviewResponse(BaseModel):
    id: int
    persona_name: str
    repo: str
    pr_number: int
    pr_url: str
    verdict: str
    comment_count: int
    created_at: str
    duration_ms: int

class PersonaSummary(BaseModel):
    name: str
    github_user: str
    last_updated: str
    priority_count: int

class PersonaDetail(BaseModel):
    name: str
    github_user: str
    mined_from: str
    last_updated: str
    priorities: list[dict]
    pet_peeves: list[str]
    tone: str

class JobResponse(BaseModel):
    id: str
    owner: str
    repo: str
    pr_number: int
    persona_name: str
    status: str
    queued_at: str
    started_at: str | None
    completed_at: str | None
    error_message: str | None

class MineRequest(BaseModel):
    github_user: str
    full: bool = False

class MineResponse(BaseModel):
    status: str = "mining_started"
    persona_name: str

from typing import Generic, TypeVar

T = TypeVar("T")

class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    per_page: int
    pages: int
```

#### 4. Add API key management CLI

**`review_bot/cli/main.py`**

Add `api-key` command group:

```python
@cli.group()
def api_key():
    """Manage API keys for the programmatic API."""

@api_key.command()
@click.option("--label", required=True, help="Human-readable label for this key")
def create(label: str):
    """Generate a new API key."""

@api_key.command()
def list():
    """List all API keys (shows labels and creation dates, not raw keys)."""

@api_key.command()
@click.argument("key_id")
def revoke(key_id: str):
    """Revoke an API key."""
```

#### 5. Wire routes into FastAPI app

**`review_bot/server/app.py`**

```python
from review_bot.server.routes import reviews, personas, jobs

app.include_router(router)  # existing webhook router
app.include_router(reviews.router)
app.include_router(personas.router)
app.include_router(jobs.router)
```

### Database Changes

New table for API key storage:

```sql
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `REVIEW_BOT_API_ENABLED` | `true` | Enable/disable the REST API |
| `REVIEW_BOT_API_RATE_LIMIT` | `100` | Requests per minute per API key |

CLI commands: `review-bot api-key create --label "CI pipeline"`, `review-bot api-key list`, `review-bot api-key revoke <id>`.

### Testing Strategy

- Unit test each route with `httpx.AsyncClient` + `TestClient` from FastAPI
- Test API key auth: valid key → 200, missing key → 401, revoked key → 401
- Test pagination: verify `total`, `page`, `pages` math
- Test review trigger: mock job queue and verify job is enqueued
- Test persona CRUD against a temporary `PersonaStore` directory
- OpenAPI schema snapshot test to detect breaking API changes

### Estimated Effort

🟡 Medium (2–3 days)

- 0.5 days: auth + schemas
- 1 day: route implementations
- 0.5 days: CLI + wiring
- 0.5 days: tests + OpenAPI docs

---

## 5. Plugin System for Custom Review Rules

### Overview

Allow users to write Python plugins that add custom review rules (e.g., "flag any SQL query without parameterized inputs" or "require error handling in all API endpoints"). Plugins hook into the review pipeline between repo scanning and prompt building, injecting extra context or constraints into the LLM prompt.

### Current State

- `ReviewOrchestrator` in `review_bot/review/orchestrator.py` runs a linear pipeline: persona → PR fetch → scan → prompt → review → format → post
- `PromptBuilder` in `review_bot/review/prompt_builder.py` builds the prompt from persona, repo context, PR data, and diff — no extension points
- The `SYSTEM_PROMPT_TEMPLATE` has an `{overrides_text}` placeholder from persona overrides, which could serve as an injection point
- `RepoScanner` detects repo conventions (languages, frameworks, linters) but doesn't run custom analysis
- No plugin discovery, loading, or sandboxing infrastructure exists

### Implementation Steps

#### 1. Define the plugin hook API

**`review_bot/plugins/__init__.py`**

```python
from review_bot.plugins.base import ReviewPlugin, PluginContext, PluginResult
from review_bot.plugins.registry import PluginRegistry
```

**`review_bot/plugins/base.py`**

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

@dataclass
class PluginContext:
    """Context passed to plugins during review."""
    diff: str
    files: list[ChangedFile]
    repo_context: RepoContext
    persona: PersonaProfile
    pr_data: dict

@dataclass
class PluginResult:
    """Result from a plugin's analysis."""
    extra_instructions: list[str] = field(default_factory=list)
    flagged_patterns: list[FlaggedPattern] = field(default_factory=list)
    skip_files: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

@dataclass
class FlaggedPattern:
    """A specific code pattern flagged by a plugin."""
    file: str
    line: int | None
    rule_id: str
    message: str
    severity: str  # "error" | "warning" | "info"

class ReviewPlugin(ABC):
    """Base class for review plugins.

    Plugins must implement `analyze()` and provide metadata via class attributes.
    """

    name: str = "unnamed-plugin"
    version: str = "0.1.0"
    description: str = ""

    @abstractmethod
    async def analyze(self, context: PluginContext) -> PluginResult:
        """Analyze the PR and return findings.

        Args:
            context: Review context including diff, files, and persona.

        Returns:
            Plugin results with extra instructions and flagged patterns.
        """
        ...

    def should_run(self, context: PluginContext) -> bool:
        """Override to conditionally skip this plugin.

        Default: always run.
        """
        return True
```

#### 2. Implement plugin discovery and registry

**`review_bot/plugins/registry.py`**

```python
class PluginRegistry:
    """Discovers, loads, and manages review plugins."""

    def __init__(self) -> None:
        self._plugins: list[ReviewPlugin] = []

    def register(self, plugin: ReviewPlugin) -> None:
        """Register a plugin instance."""
        self._plugins.append(plugin)
        logger.info("Registered plugin: %s v%s", plugin.name, plugin.version)

    def discover_from_entry_points(self, group: str = "review_bot.plugins") -> None:
        """Discover plugins via Python entry points (setuptools/hatch)."""
        from importlib.metadata import entry_points

        eps = entry_points(group=group)
        for ep in eps:
            try:
                plugin_class = ep.load()
                self.register(plugin_class())
            except Exception:
                logger.warning("Failed to load plugin: %s", ep.name, exc_info=True)

    def discover_from_directory(self, path: Path) -> None:
        """Discover plugins from Python files in a directory."""
        if not path.is_dir():
            return
        for py_file in sorted(path.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"review_bot_plugin_{py_file.stem}", py_file,
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                # Find ReviewPlugin subclasses
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (isinstance(attr, type) and issubclass(attr, ReviewPlugin)
                            and attr is not ReviewPlugin):
                        self.register(attr())
            except Exception:
                logger.warning("Failed to load plugin from %s", py_file, exc_info=True)

    async def run_all(self, context: PluginContext) -> list[PluginResult]:
        """Run all registered plugins and collect results."""
        results = []
        for plugin in self._plugins:
            if not plugin.should_run(context):
                logger.debug("Skipping plugin %s (should_run=False)", plugin.name)
                continue
            try:
                result = await asyncio.wait_for(
                    plugin.analyze(context),
                    timeout=30.0,  # Per-plugin timeout
                )
                results.append(result)
                logger.info(
                    "Plugin %s: %d instructions, %d flags",
                    plugin.name,
                    len(result.extra_instructions),
                    len(result.flagged_patterns),
                )
            except asyncio.TimeoutError:
                logger.warning("Plugin %s timed out after 30s", plugin.name)
            except Exception:
                logger.warning("Plugin %s failed", plugin.name, exc_info=True)
        return results
```

#### 3. Integrate plugins into the review pipeline

**`review_bot/review/orchestrator.py`**

Add plugin execution between repo scanning and prompt building:

```python
class ReviewOrchestrator:
    def __init__(
        self,
        github_client: GitHubAPIClient,
        persona_store: PersonaStore,
        db_engine: AsyncEngine | None = None,
        plugin_registry: PluginRegistry | None = None,
    ) -> None:
        self._plugins = plugin_registry or PluginRegistry()

    async def run_review(self, ...) -> ReviewResult:
        # ... existing steps 1-3 ...

        # 3.5 Run plugins
        plugin_context = PluginContext(
            diff=diff, files=files, repo_context=repo_context,
            persona=persona, pr_data=pr_data,
        )
        plugin_results = await self._plugins.run_all(plugin_context)

        # 4. Build prompt (now with plugin results)
        prompt = self._prompt_builder.build(
            persona=persona,
            repo_context=repo_context,
            pr_data=pr_data,
            diff=diff,
            files=files,
            plugin_results=plugin_results,  # NEW
        )
```

**`review_bot/review/prompt_builder.py`**

Add plugin results to the prompt template:

```python
class PromptBuilder:
    def build(
        self,
        persona: PersonaProfile,
        repo_context: RepoContext,
        pr_data: dict,
        diff: str,
        files: list[PullRequestFile],
        plugin_results: list[PluginResult] | None = None,
    ) -> str:
        # ... existing logic ...
        plugin_text = self._format_plugin_results(plugin_results or [])
        # Insert before the diff section

    def _format_plugin_results(self, results: list[PluginResult]) -> str:
        """Format plugin findings as additional review context."""
        instructions = []
        flags = []
        for r in results:
            instructions.extend(r.extra_instructions)
            flags.extend(r.flagged_patterns)

        if not instructions and not flags:
            return ""

        parts = ["## Plugin Analysis\n"]
        if instructions:
            parts.append("**Additional review instructions:**")
            for i in instructions:
                parts.append(f"- {i}")
            parts.append("")
        if flags:
            parts.append("**Flagged patterns (review these carefully):**")
            for f in flags:
                loc = f"{f.file}:{f.line}" if f.line else f.file
                parts.append(f"- [{f.severity.upper()}] {loc}: {f.message} ({f.rule_id})")
            parts.append("")

        return "\n".join(parts)
```

#### 4. Add built-in example plugins

**`review_bot/plugins/builtin/sql_injection.py`**

```python
class SQLInjectionPlugin(ReviewPlugin):
    name = "sql-injection-detector"
    version = "0.1.0"
    description = "Flags potential SQL injection vulnerabilities"

    SQL_PATTERNS = [
        re.compile(r'f"[^"]*(?:SELECT|INSERT|UPDATE|DELETE|DROP)[^"]*\{', re.IGNORECASE),
        re.compile(r"f'[^']*(?:SELECT|INSERT|UPDATE|DELETE|DROP)[^']*\{", re.IGNORECASE),
        re.compile(r'\.execute\(\s*f["\']', re.IGNORECASE),
        re.compile(r'%s.*%\s*\(', re.IGNORECASE),
    ]

    async def analyze(self, context: PluginContext) -> PluginResult:
        flags = []
        for line_no, line in enumerate(context.diff.split("\n"), 1):
            if not line.startswith("+"):
                continue
            for pattern in self.SQL_PATTERNS:
                if pattern.search(line):
                    flags.append(FlaggedPattern(
                        file="(from diff)",
                        line=line_no,
                        rule_id="SQL001",
                        message="Possible SQL injection: use parameterized queries",
                        severity="error",
                    ))
        return PluginResult(flagged_patterns=flags)
```

#### 5. Plugin configuration

**`review_bot/config/settings.py`**

```python
class Settings(BaseSettings):
    plugins_dir: Path = Field(
        default=CONFIG_DIR / "plugins",
        description="Directory to discover plugins from",
    )
    plugins_enabled: list[str] = Field(
        default_factory=list,
        description="List of plugin names to enable (empty = all)",
    )
    plugins_timeout: int = Field(
        default=30,
        description="Per-plugin timeout in seconds",
    )
```

### Database Changes

None — plugin results are injected into the prompt, not stored separately. Plugin metadata could optionally be logged in the `reviews` table via a `plugins_run` JSON column in a future iteration.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `REVIEW_BOT_PLUGINS_DIR` | `~/.review-bot/plugins/` | Plugin discovery directory |
| `REVIEW_BOT_PLUGINS_ENABLED` | `[]` (all) | Allowlist of plugin names |
| `REVIEW_BOT_PLUGINS_TIMEOUT` | `30` | Per-plugin timeout in seconds |

Entry point group: `review_bot.plugins` — third-party packages register via `pyproject.toml`:

```toml
[project.entry-points."review_bot.plugins"]
my-plugin = "my_package.plugin:MyPlugin"
```

### Testing Strategy

- Unit test `ReviewPlugin` subclass with mock `PluginContext`
- Test `PluginRegistry.discover_from_directory()` with temp directory containing plugin files
- Test `PluginRegistry.discover_from_entry_points()` with mock entry points
- Test timeout handling: plugin that sleeps 60s should be killed at 30s
- Test plugin error isolation: failing plugin should not block others
- Integration test: SQL injection plugin flags known bad patterns in a fixture diff
- Test `PromptBuilder._format_plugin_results()` output format

### Estimated Effort

🔴 Large (4–5 days)

- 1 day: plugin base classes + registry
- 1 day: pipeline integration (orchestrator + prompt builder)
- 1 day: built-in plugins + entry point discovery
- 1 day: sandboxing + timeout + error isolation
- 0.5 days: tests + documentation

---

## 6. Self-Hosted Deployment Guide

### Overview

Production-ready Docker images and Kubernetes manifests for deploying review-like-him. Includes a multi-stage Dockerfile, docker-compose for small teams, Helm chart for larger organizations, health check probes, secrets management, and horizontal scaling guidance.

### Current State

- No Dockerfile exists in the repository
- No container or orchestration configuration
- `setup.sh` handles local development setup only
- `pyproject.toml` defines dependencies via `hatchling` build system with `review-bot` as the CLI entry point
- `Settings` in `review_bot/config/settings.py` loads config from `REVIEW_BOT_*` environment variables and `.env` files
- The server binds to `0.0.0.0:8000` by default
- Database defaults to SQLite at `~/.review-bot/data/review_bot.db`
- GitHub App private key defaults to `~/.review-bot/private-key.pem`

### Implementation Steps

#### 1. Create multi-stage Dockerfile

**`Dockerfile`**

```dockerfile
# ---- Build stage ----
FROM python:3.11-slim AS builder

WORKDIR /app
COPY pyproject.toml README.md ./
COPY review_bot/ review_bot/

RUN pip install --no-cache-dir build \
    && python -m build --wheel \
    && pip install --no-cache-dir dist/*.whl

# ---- Runtime stage ----
FROM python:3.11-slim AS runtime

RUN groupadd --gid 1000 reviewbot \
    && useradd --uid 1000 --gid 1000 --create-home reviewbot

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/review-bot /usr/local/bin/review-bot

# Create data directories
RUN mkdir -p /data/personas /data/plugins \
    && chown -R reviewbot:reviewbot /data

USER reviewbot

ENV REVIEW_BOT_HOST=0.0.0.0 \
    REVIEW_BOT_PORT=8000 \
    REVIEW_BOT_DB_URL=sqlite+aiosqlite:///data/review_bot.db \
    REVIEW_BOT_PRIVATE_KEY_PATH=/secrets/private-key.pem

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"

ENTRYPOINT ["review-bot"]
CMD ["serve"]
```

#### 2. Create docker-compose for small teams

**`docker-compose.yml`**

```yaml
version: "3.9"

services:
  review-bot:
    build: .
    ports:
      - "8000:8000"
    environment:
      REVIEW_BOT_GITHUB_APP_ID: "${GITHUB_APP_ID}"
      REVIEW_BOT_WEBHOOK_SECRET: "${WEBHOOK_SECRET}"
      REVIEW_BOT_DB_URL: "sqlite+aiosqlite:///data/review_bot.db"
    volumes:
      - review-data:/data
      - ./secrets/private-key.pem:/secrets/private-key.pem:ro
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"]
      interval: 30s
      timeout: 5s
      retries: 3

  # Optional: PostgreSQL for multi-instance deployments
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: review_bot
      POSTGRES_USER: review_bot
      POSTGRES_PASSWORD: "${POSTGRES_PASSWORD}"
    volumes:
      - pg-data:/var/lib/postgresql/data
    profiles:
      - postgres

volumes:
  review-data:
  pg-data:
```

**`docker-compose.postgres.yml`** — override for PostgreSQL:

```yaml
version: "3.9"

services:
  review-bot:
    environment:
      REVIEW_BOT_DB_URL: "postgresql+asyncpg://review_bot:${POSTGRES_PASSWORD}@postgres:5432/review_bot"
    depends_on:
      postgres:
        condition: service_healthy
```

#### 3. Create Helm chart

**`deploy/helm/review-bot/Chart.yaml`**

```yaml
apiVersion: v2
name: review-bot
description: AI-powered code review bot that mimics real reviewers
type: application
version: 0.1.0
appVersion: "0.1.0"
```

**`deploy/helm/review-bot/values.yaml`**

```yaml
replicaCount: 1

image:
  repository: ghcr.io/review-like-him/review-bot
  tag: latest
  pullPolicy: IfNotPresent

service:
  type: ClusterIP
  port: 8000

ingress:
  enabled: false
  className: nginx
  hosts:
    - host: review-bot.example.com
      paths:
        - path: /
          pathType: Prefix

resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    cpu: 500m
    memory: 512Mi

persistence:
  enabled: true
  size: 1Gi
  storageClass: ""

database:
  type: sqlite  # or "postgresql"
  postgresql:
    host: ""
    port: 5432
    database: review_bot
    existingSecret: ""

github:
  appId: ""
  webhookSecret:
    existingSecret: ""
    secretKey: webhook-secret
  privateKey:
    existingSecret: ""
    secretKey: private-key.pem

autoscaling:
  enabled: false
  minReplicas: 1
  maxReplicas: 5
  targetCPUUtilization: 80
```

**`deploy/helm/review-bot/templates/deployment.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "review-bot.fullname" . }}
spec:
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      app: {{ include "review-bot.name" . }}
  template:
    metadata:
      labels:
        app: {{ include "review-bot.name" . }}
    spec:
      containers:
        - name: review-bot
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          ports:
            - containerPort: 8000
          env:
            - name: REVIEW_BOT_GITHUB_APP_ID
              value: "{{ .Values.github.appId }}"
            - name: REVIEW_BOT_WEBHOOK_SECRET
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.github.webhookSecret.existingSecret }}
                  key: {{ .Values.github.webhookSecret.secretKey }}
            - name: REVIEW_BOT_PRIVATE_KEY_PATH
              value: /secrets/private-key.pem
          volumeMounts:
            - name: github-private-key
              mountPath: /secrets
              readOnly: true
            - name: data
              mountPath: /data
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
      volumes:
        - name: github-private-key
          secret:
            secretName: {{ .Values.github.privateKey.existingSecret }}
        - name: data
          {{- if .Values.persistence.enabled }}
          persistentVolumeClaim:
            claimName: {{ include "review-bot.fullname" . }}
          {{- else }}
          emptyDir: {}
          {{- end }}
```

#### 4. Create Kubernetes manifests (non-Helm)

**`deploy/k8s/namespace.yaml`**
**`deploy/k8s/deployment.yaml`**
**`deploy/k8s/service.yaml`**
**`deploy/k8s/ingress.yaml`**
**`deploy/k8s/secrets.yaml`** (template with placeholder values)
**`deploy/k8s/pvc.yaml`**

Plain Kubernetes YAML for teams that don't use Helm.

#### 5. Add .dockerignore

**`.dockerignore`**

```
.git
.venv
__pycache__
*.pyc
.env
tests/
docs/
*.md
.ruff_cache
.pytest_cache
.mypy_cache
deploy/
```

#### 6. Document deployment patterns

**`deploy/README.md`**

Cover three deployment patterns:

1. **docker-compose** (small team, single server): `docker compose up -d`
2. **docker-compose + PostgreSQL** (multi-worker): `docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d`
3. **Kubernetes + Helm** (production): `helm install review-bot deploy/helm/review-bot/ -f values-prod.yaml`

Include:
- Secrets management (GitHub App private key, webhook secret, API keys)
- Persistent storage for SQLite mode
- PostgreSQL connection pooling for multi-replica
- Ingress/TLS configuration for webhook reception
- Resource sizing recommendations
- Horizontal scaling notes (stateless workers with shared PostgreSQL)
- Backup strategy for persona YAML files

### Database Changes

None — deployment configuration doesn't change the schema.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `REVIEW_BOT_HOST` | `0.0.0.0` | Server bind host |
| `REVIEW_BOT_PORT` | `8000` | Server bind port |
| `REVIEW_BOT_DB_URL` | `sqlite+aiosqlite:///data/review_bot.db` | Database URL |
| `REVIEW_BOT_PRIVATE_KEY_PATH` | `/secrets/private-key.pem` | Private key mount path |

All existing `REVIEW_BOT_*` variables work in containers via `env` or `envFrom`.

### Testing Strategy

- Build the Docker image and verify it starts: `docker build -t review-bot . && docker run --rm review-bot --help`
- Verify health check endpoint responds inside container
- Test docker-compose stack brings up the service and postgres (if profiled)
- Helm chart lint: `helm lint deploy/helm/review-bot/`
- Helm template render: `helm template review-bot deploy/helm/review-bot/` and validate YAML
- Test with `kind` (Kubernetes in Docker) for full k8s deployment smoke test
- Verify non-root user: container runs as UID 1000
- Verify secrets mount correctly and private key permissions are enforced

### Estimated Effort

🟡 Medium (2–3 days)

- 0.5 days: Dockerfile + .dockerignore
- 0.5 days: docker-compose configurations
- 1 day: Helm chart + plain k8s manifests
- 0.5 days: documentation + testing

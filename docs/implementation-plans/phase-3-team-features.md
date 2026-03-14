# Phase 3 — Team Features: Implementation Plan

> **Status:** Draft
> **Date:** 2026-03-15
> **Depends on:** Phase 1 (health check, graceful shutdown), Phase 2 (severity filtering, confidence scores)

This document provides exhaustive implementation details for all six Phase 3 roadmap items. Each section includes file-by-file changes, API/CLI interface design, data model changes, edge cases, security considerations, deployment notes, and testing approach.

---

## Table of Contents

1. [Team Dashboard (Web UI)](#1-team-dashboard-web-ui)
2. [Multiple Persona Assignment](#2-multiple-persona-assignment)
3. [Persona Comparison](#3-persona-comparison)
4. [Review Templates per Repo/Team](#4-review-templates-per-repoteam)
5. [Slack/Discord Notifications](#5-slackdiscord-notifications)
6. [GitHub Actions Integration](#6-github-actions-integration)

---

## 1. Team Dashboard (Web UI)

### 1.1 Architecture Decision

**Recommendation: FastAPI + Jinja2 templates for v1.**

| Option | Pros | Cons |
|--------|------|------|
| FastAPI + Jinja2 | Zero new build tooling, ships with existing server, no CORS config, SSR for fast first paint | Limited interactivity, harder to add complex client-side features later |
| React SPA | Rich interactivity, component reuse, ecosystem | Requires separate build pipeline (Vite/webpack), CORS setup, API versioning, doubles deployment surface |

For v1, Jinja2 templates with HTMX for progressive enhancement strike the best balance. The dashboard is read-heavy (analytics, status) with minimal write interactions (configuration). A React SPA can replace individual pages later without changing the API layer.

### 1.2 Pages Needed

| Page | Route | Description |
|------|-------|-------------|
| Overview | `GET /dashboard/` | Review count (24h/7d/30d), active personas, queue depth, worker status |
| Activity Timeline | `GET /dashboard/activity` | Chronological list of reviews with persona, repo, verdict, duration |
| Persona Trends | `GET /dashboard/personas` | Per-persona accuracy trends, review count over time, avg comment count |
| Queue Status | `GET /dashboard/queue` | Active/queued/failed jobs, retry controls |
| Configuration | `GET /dashboard/config` | Current settings (read-only), persona list, repo assignments |

### 1.3 File-by-File Changes

#### New Files

**`review_bot/dashboard/__init__.py`**
```python
from __future__ import annotations
```

**`review_bot/dashboard/router.py`** — FastAPI router for all dashboard routes.
```python
from __future__ import annotations

import logging
from datetime import datetime, timedelta, UTC

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("review-bot")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory="review_bot/dashboard/templates")


def _get_engine(request: Request) -> AsyncEngine:
    return request.app.state.db_engine


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request, engine: AsyncEngine = Depends(_get_engine)) -> HTMLResponse:
    """Dashboard overview: review counts, active personas, queue depth."""
    ...


@router.get("/activity", response_class=HTMLResponse)
async def activity(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    persona: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    engine: AsyncEngine = Depends(_get_engine),
) -> HTMLResponse:
    """Paginated review activity timeline with filters."""
    ...


@router.get("/personas", response_class=HTMLResponse)
async def persona_trends(request: Request, engine: AsyncEngine = Depends(_get_engine)) -> HTMLResponse:
    """Per-persona accuracy trends and review statistics."""
    ...


@router.get("/queue", response_class=HTMLResponse)
async def queue_status(request: Request, engine: AsyncEngine = Depends(_get_engine)) -> HTMLResponse:
    """Current queue state: active, queued, and recently failed jobs."""
    ...


@router.get("/config", response_class=HTMLResponse)
async def configuration(request: Request, engine: AsyncEngine = Depends(_get_engine)) -> HTMLResponse:
    """Read-only view of current server configuration and personas."""
    ...
```

**`review_bot/dashboard/queries.py`** — Pure SQL query functions returning dicts.
```python
from __future__ import annotations

from datetime import datetime, timedelta, UTC

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def get_review_counts(engine: AsyncEngine) -> dict[str, int]:
    """Return review counts for 24h, 7d, and 30d windows."""
    now = datetime.now(tz=UTC)
    counts: dict[str, int] = {}
    async with engine.connect() as conn:
        for label, delta in [("24h", timedelta(hours=24)), ("7d", timedelta(days=7)), ("30d", timedelta(days=30))]:
            row = await conn.execute(
                text("SELECT COUNT(*) FROM reviews WHERE created_at >= :since"),
                {"since": (now - delta).isoformat()},
            )
            counts[label] = row.scalar_one()
    return counts


async def get_activity_page(
    engine: AsyncEngine,
    *,
    page: int = 1,
    per_page: int = 50,
    persona: str | None = None,
    repo: str | None = None,
) -> tuple[list[dict], int]:
    """Paginated activity list. Returns (rows, total_count)."""
    ...


async def get_persona_stats(engine: AsyncEngine) -> list[dict]:
    """Aggregate stats per persona: total reviews, avg comments, avg duration."""
    ...


async def get_queue_snapshot(engine: AsyncEngine) -> dict[str, list[dict]]:
    """Return queued, running, and recent failed jobs."""
    ...


async def get_reviews_per_day(engine: AsyncEngine, persona: str | None = None, days: int = 30) -> list[dict]:
    """Reviews per day for chart rendering. Returns [{date, count}, ...]."""
    ...
```

**`review_bot/dashboard/templates/`** — Jinja2 HTML templates directory:
- `base.html` — Shared layout (nav, CSS, footer)
- `overview.html` — Dashboard home
- `activity.html` — Activity timeline with pagination
- `personas.html` — Persona trend charts
- `queue.html` — Queue status table
- `config.html` — Configuration viewer

**`review_bot/dashboard/static/`** — Static assets:
- `style.css` — Minimal custom CSS (use a classless CSS framework like Pico CSS for v1)
- `charts.js` — Lightweight charting via Chart.js CDN or inline SVG generation

#### Modified Files

**`review_bot/server/app.py`**
- Import and mount the dashboard router: `from review_bot.dashboard.router import router as dashboard_router`
- Mount static files: `app.mount("/static", StaticFiles(directory="review_bot/dashboard/static"), name="static")`
- Add `app.include_router(dashboard_router)` after the webhook router
- Add `Jinja2Templates` to dependencies in `pyproject.toml`

**`pyproject.toml`**
- Add `jinja2` to dependencies
- Add `python-multipart` if form handling is needed later

### 1.4 Database Queries for Analytics

```sql
-- Reviews per persona over time (30-day window, grouped by day)
SELECT
    persona_name,
    DATE(created_at) AS review_date,
    COUNT(*) AS review_count,
    AVG(comment_count) AS avg_comments,
    AVG(duration_ms) AS avg_duration_ms
FROM reviews
WHERE created_at >= :since
GROUP BY persona_name, DATE(created_at)
ORDER BY review_date DESC;

-- Queue depth by status
SELECT status, COUNT(*) AS count
FROM jobs
WHERE status IN ('queued', 'running')
GROUP BY status;

-- Recently failed jobs (last 24h)
SELECT id, owner, repo, pr_number, persona_name, error_message, completed_at
FROM jobs
WHERE status = 'failed' AND completed_at >= :since
ORDER BY completed_at DESC
LIMIT 50;

-- Persona leaderboard
SELECT
    persona_name,
    COUNT(*) AS total_reviews,
    SUM(CASE WHEN verdict = 'approve' THEN 1 ELSE 0 END) AS approvals,
    SUM(CASE WHEN verdict = 'request_changes' THEN 1 ELSE 0 END) AS change_requests,
    AVG(comment_count) AS avg_comments,
    AVG(duration_ms) AS avg_duration_ms
FROM reviews
GROUP BY persona_name
ORDER BY total_reviews DESC;
```

### 1.5 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| **Empty state (new installation)** | Each template checks for empty query results and renders a friendly "No data yet" card with a call-to-action linking to the CLI docs for creating personas and running first reviews. |
| **Timezone handling in charts** | Store all timestamps as UTC ISO 8601 (already the case). Render charts in UTC with a `(UTC)` label. Future: add a `?tz=America/New_York` query param and convert in the template using `zoneinfo.ZoneInfo`. |
| **Large datasets / pagination** | All list endpoints accept `page` and `per_page` (max 200). Queries use `LIMIT :limit OFFSET :offset` with a `COUNT(*)` subquery for total. Chart queries aggregate server-side (no raw row transfer). Add `idx_reviews_created_at` index (already exists). |
| **Stale data if worker is down** | The queue status page queries the `jobs` table for jobs stuck in `running` status longer than a configurable threshold (default 10 minutes). Display a warning banner: "Worker may be down — N jobs have been running for >10 minutes." The overview page shows the worker task's alive status from `app.state.job_queue._worker_task.done()`. |
| **Auth for dashboard access** | v1: No auth (same trust model as the webhook endpoint — assumes network-level access control). Add a `REVIEW_BOT_DASHBOARD_TOKEN` setting checked via a simple middleware or dependency that returns 401 if the `Authorization: Bearer <token>` header is missing or wrong. Document that production deployments should use a reverse proxy (nginx, Caddy) with auth or restrict network access. |

### 1.6 Static File Serving

Mount via FastAPI's `StaticFiles`:
```python
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="review_bot/dashboard/static"), name="static")
```

For production, recommend serving static files via a reverse proxy (nginx) and setting `Cache-Control` headers. The `StaticFiles` mount handles development serving.

### 1.7 Security Considerations

- Dashboard token auth via `REVIEW_BOT_DASHBOARD_TOKEN` env var — bearer token in header or cookie
- No write operations in v1 dashboard (read-only SQL queries only)
- SQL queries use parameterized `text()` bindings (SQLAlchemy) — no injection risk
- Template auto-escaping enabled by default in Jinja2
- Rate limit dashboard endpoints separately from webhook endpoints (future)

### 1.8 Deployment Notes

- Jinja2 templates are bundled in the Python package — no separate build step
- Static files included via `package_data` in `pyproject.toml`
- Dashboard adds ~200KB to the package (CSS framework + Chart.js CDN link)
- No additional services required — runs in the existing FastAPI process

### 1.9 Testing Approach

**`tests/test_dashboard.py`**

- Use `httpx.AsyncClient` with `ASGITransport(app=app)` for integration tests
- Test each route returns 200 with empty database (empty state rendering)
- Test pagination: insert 100+ review rows, verify page boundaries
- Test query filters: `?persona=deepam` filters correctly
- Test dashboard token auth: requests without token return 401 when configured
- Mock `app.state.db_engine` with an in-memory SQLite engine via `conftest.py` fixture

**`tests/test_dashboard_queries.py`**

- Unit test each query function with a pre-seeded in-memory SQLite database
- Test `get_review_counts` with reviews at various timestamps
- Test `get_activity_page` pagination math and filter combinations
- Test `get_queue_snapshot` with jobs in various statuses
- Test edge case: empty tables return sensible defaults (0 counts, empty lists)

---

## 2. Multiple Persona Assignment

### 2.1 Overview

Allow a single PR to receive independent reviews from 2+ personas. A `pull_request.review_requested` event or `/review-as deepam sarah` comment fans out into multiple queued jobs.

### 2.2 File-by-File Changes

#### Modified Files

**`review_bot/server/webhooks.py`**

The existing `_handle_issue_comment` already loops over multiple persona names from `/review-as` commands. Changes needed:

1. Add deduplication: track `(repo, pr_number, persona_name)` tuples within a single event to avoid double-queuing.
2. Add a maximum persona limit per PR (configurable, default 5).
3. Add fan-out for `_handle_review_requested` — currently handles a single requested reviewer. Extend to check for multiple `requested_reviewers` in the payload.
4. Add fan-out for `_handle_label_event` — check all labels on the PR, not just the newly added one.

```python
# New constant
MAX_PERSONAS_PER_PR: int = 5

# New deduplication helper
async def _deduplicated_enqueue(
    owner: str,
    repo: str,
    pr_number: int,
    persona_names: list[str],
    installation_id: int,
) -> list[str]:
    """Enqueue review jobs for unique persona names, respecting the limit.

    Returns list of actually enqueued persona names.
    """
    seen: set[str] = set()
    enqueued: list[str] = []
    for name in persona_names:
        if name in seen:
            logger.warning("Duplicate persona '%s' for PR #%d, skipping", name, pr_number)
            continue
        if len(enqueued) >= MAX_PERSONAS_PER_PR:
            logger.warning(
                "Max persona limit (%d) reached for PR #%d, skipping '%s'",
                MAX_PERSONAS_PER_PR, pr_number, name,
            )
            break
        seen.add(name)
        if not await _persona_exists(name):
            await _post_persona_not_found(owner, repo, pr_number, installation_id, name)
            continue
        await _enqueue_review(owner, repo, pr_number, name, installation_id)
        enqueued.append(name)
    return enqueued
```

Update `_handle_review_requested` to handle the `requested_reviewers` (plural) field from the webhook payload in addition to `requested_reviewer` (singular).

Update `_handle_issue_comment` to use `_deduplicated_enqueue`.

**`review_bot/server/queue.py`**

Add in-flight deduplication to prevent re-queuing a persona that's already reviewing the same PR:

```python
async def enqueue(self, job: ReviewJob) -> str | None:
    """Add a review job, skipping if an identical job is already queued/running."""
    if await self._is_duplicate(job):
        logger.info(
            "Skipping duplicate job: %s/%s#%d as '%s'",
            job.owner, job.repo, job.pr_number, job.persona_name,
        )
        return None
    await self._persist_job(job)
    await self._queue.put(job)
    ...


async def _is_duplicate(self, job: ReviewJob) -> bool:
    """Check if a job with the same repo/PR/persona is already queued or running."""
    async with self._db_engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT COUNT(*) FROM jobs "
                "WHERE owner = :owner AND repo = :repo "
                "AND pr_number = :pr AND persona_name = :persona "
                "AND status IN ('queued', 'running')"
            ),
            {
                "owner": job.owner, "repo": job.repo,
                "pr": job.pr_number, "persona": job.persona_name,
            },
        )
        return row.scalar_one() > 0
```

**`review_bot/config/settings.py`**

Add setting:
```python
max_personas_per_pr: int = Field(
    default=5,
    description="Maximum number of personas that can review a single PR",
)
```

**`review_bot/review/github_poster.py`**

Add a configurable delay between posting multiple reviews to avoid GitHub API rate limiting:

```python
MULTI_REVIEW_DELAY_SECONDS: float = 2.0  # delay between posting reviews for same PR
```

The queue worker processes jobs sequentially, so reviews for the same PR from different personas are naturally staggered. If parallel workers are added later, introduce a per-PR lock.

### 2.3 Data Model Changes

No schema changes required. The existing `jobs` and `reviews` tables already store `persona_name` per row, supporting multiple reviews per PR naturally.

Add a composite index for the deduplication query:

```sql
CREATE INDEX IF NOT EXISTS idx_jobs_dedup
ON jobs(owner, repo, pr_number, persona_name, status);
```

### 2.4 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| **Same persona assigned twice** | `_deduplicated_enqueue` uses a `seen` set to skip duplicates within a single event. `_is_duplicate` in the queue catches cross-event duplicates by checking for queued/running jobs with the same `(owner, repo, pr_number, persona_name)`. |
| **One persona fails while others succeed (partial review state)** | Each persona's job is independent. A failure in one does not affect others. The failed job posts its own error comment on the PR. The `jobs` table tracks individual status per persona. The dashboard shows per-job status so teams can see which persona failed. |
| **Queue ordering with multiple jobs for same PR** | Jobs are FIFO in the asyncio queue. Multiple personas for the same PR are enqueued in the order they appear in the command/payload. No priority reordering — simplicity over optimization. If a team needs priority, they can configure persona order in the repo config (Phase 3.4). |
| **Rate limiting when posting multiple reviews rapidly** | The existing exponential backoff in `GitHubAPIClient._request` handles 403/rate-limit responses. Additionally, the sequential worker loop naturally spaces out reviews. Add a `MULTI_REVIEW_DELAY_SECONDS` (default 2s) sleep in the worker between consecutive jobs targeting the same PR. |
| **Personas contradict each other** | This is expected and valuable — it's a feature, not a bug. Each persona posts independently with its own verdict. The PR author sees both perspectives. No conflict resolution is attempted. Document this as intentional in the review header: "Note: Multiple persona reviews are independent — differing opinions reflect different reviewer perspectives." |
| **Maximum persona limit per PR** | Enforced in `_deduplicated_enqueue` with `MAX_PERSONAS_PER_PR` (configurable via settings, default 5). When exceeded, a warning comment is posted: "Maximum of 5 personas per PR. The following were skipped: ..." |

### 2.5 API/CLI Interface Design

No new CLI commands needed. Existing trigger mechanisms support multi-persona naturally:

- `/review-as deepam sarah mike` — already parsed by `_parse_review_command`
- Labels: `review:deepam` + `review:sarah` — each label event fires independently
- Reviewer assignment: multiple bot accounts can be assigned (though typically there's one GitHub App)

### 2.6 Security Considerations

- Persona limit prevents abuse (queuing hundreds of reviews per PR)
- Deduplication prevents accidental DoS from webhook retries
- Each persona job uses the same installation token — no additional auth needed

### 2.7 Deployment Notes

- No new services or dependencies
- The deduplication index should be created via the existing `_CREATE_INDEXES_SQL` list in `app.py`
- Rolling deploy safe — old code simply doesn't deduplicate (harmless double reviews)

### 2.8 Testing Approach

**`tests/test_webhooks.py`** (extend existing)

- Test `/review-as deepam deepam` deduplicates to one job
- Test `/review-as a b c d e f` respects `MAX_PERSONAS_PER_PR` limit
- Test `_handle_review_requested` with multiple `requested_reviewers`
- Test `_handle_label_event` fan-out across existing labels

**`tests/test_queue.py`** (new)

- Test `_is_duplicate` returns True for queued/running jobs with same key
- Test `_is_duplicate` returns False for completed/failed jobs (allows re-review)
- Test `enqueue` returns None for duplicate jobs
- Integration test: enqueue 3 personas for same PR, verify all 3 execute

---

## 3. Persona Comparison

### 3.1 Overview

A CLI command and API endpoint that runs a diff through multiple personas and returns structured side-by-side results without posting to GitHub. Useful for persona calibration and understanding stylistic differences.

### 3.2 File-by-File Changes

#### New Files

**`review_bot/review/comparator.py`** — Core comparison logic.

```python
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from review_bot.github.api import GitHubAPIClient
from review_bot.persona.store import PersonaStore
from review_bot.review.formatter import ReviewFormatter, ReviewResult
from review_bot.review.prompt_builder import PromptBuilder
from review_bot.review.repo_scanner import RepoScanner
from review_bot.review.reviewer import ClaudeReviewer

logger = logging.getLogger("review-bot")

# Timeout per persona review (seconds)
DEFAULT_PER_PERSONA_TIMEOUT: float = 120.0
MAX_CONCURRENT_PERSONAS: int = 3


@dataclass
class ComparisonEntry:
    """Review result from a single persona in a comparison."""
    persona_name: str
    result: ReviewResult
    duration_ms: int
    error: str | None = None


@dataclass
class ComparisonResult:
    """Side-by-side comparison of multiple persona reviews."""
    pr_url: str
    entries: list[ComparisonEntry] = field(default_factory=list)
    total_duration_ms: int = 0


class PersonaComparator:
    """Runs a PR through multiple personas without posting results."""

    def __init__(
        self,
        github_client: GitHubAPIClient,
        persona_store: PersonaStore,
    ) -> None:
        self._github = github_client
        self._persona_store = persona_store
        self._scanner = RepoScanner(github_client)
        self._prompt_builder = PromptBuilder()
        self._reviewer = ClaudeReviewer()
        self._formatter = ReviewFormatter()

    async def compare(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        persona_names: list[str],
        *,
        timeout_per_persona: float = DEFAULT_PER_PERSONA_TIMEOUT,
    ) -> ComparisonResult:
        """Run a PR through multiple personas and return comparison.

        Reviews are run concurrently (up to MAX_CONCURRENT_PERSONAS at a time)
        to reduce total wait time.
        """
        start = time.monotonic()
        pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"

        # Fetch shared data once
        pr_data = await self._github.get_pull_request(owner, repo, pr_number)
        files = await self._github.get_pull_request_files(owner, repo, pr_number)
        diff = await self._github.get_pull_request_diff(owner, repo, pr_number)
        repo_context = await self._scanner.scan(owner, repo)

        # Run personas concurrently with semaphore
        sem = asyncio.Semaphore(MAX_CONCURRENT_PERSONAS)
        entries = await asyncio.gather(
            *(
                self._review_with_persona(
                    name, pr_data, files, diff, repo_context, pr_url,
                    sem, timeout_per_persona,
                )
                for name in persona_names
            )
        )

        total_ms = int((time.monotonic() - start) * 1000)
        return ComparisonResult(
            pr_url=pr_url,
            entries=list(entries),
            total_duration_ms=total_ms,
        )

    async def _review_with_persona(
        self, persona_name, pr_data, files, diff, repo_context, pr_url,
        sem, timeout,
    ) -> ComparisonEntry:
        """Run a single persona review with timeout and error handling."""
        async with sem:
            start = time.monotonic()
            try:
                persona = self._persona_store.load(persona_name)
                prompt = self._prompt_builder.build(
                    persona=persona,
                    repo_context=repo_context,
                    pr_data=pr_data,
                    diff=diff,
                    files=files,
                )
                raw_output = await asyncio.wait_for(
                    self._reviewer.review(prompt),
                    timeout=timeout,
                )
                result = self._formatter.format(raw_output, persona_name, pr_url)
                duration_ms = int((time.monotonic() - start) * 1000)
                return ComparisonEntry(
                    persona_name=persona_name,
                    result=result,
                    duration_ms=duration_ms,
                )
            except asyncio.TimeoutError:
                duration_ms = int((time.monotonic() - start) * 1000)
                return ComparisonEntry(
                    persona_name=persona_name,
                    result=ReviewResult(
                        verdict="comment", summary_sections=[], inline_comments=[],
                        persona_name=persona_name, pr_url=pr_url,
                    ),
                    duration_ms=duration_ms,
                    error=f"Timed out after {timeout}s",
                )
            except FileNotFoundError:
                duration_ms = int((time.monotonic() - start) * 1000)
                return ComparisonEntry(
                    persona_name=persona_name,
                    result=ReviewResult(
                        verdict="comment", summary_sections=[], inline_comments=[],
                        persona_name=persona_name, pr_url=pr_url,
                    ),
                    duration_ms=duration_ms,
                    error=f"Persona '{persona_name}' not found",
                )
            except Exception as exc:
                duration_ms = int((time.monotonic() - start) * 1000)
                logger.error("Comparison failed for persona '%s': %s", persona_name, exc)
                return ComparisonEntry(
                    persona_name=persona_name,
                    result=ReviewResult(
                        verdict="comment", summary_sections=[], inline_comments=[],
                        persona_name=persona_name, pr_url=pr_url,
                    ),
                    duration_ms=duration_ms,
                    error=str(exc),
                )
```

**`review_bot/review/comparison_formatter.py`** — CLI and API output formatting.

```python
from __future__ import annotations

from review_bot.review.comparator import ComparisonResult


def format_comparison_cli(result: ComparisonResult) -> str:
    """Format a ComparisonResult for CLI terminal output.

    Produces a side-by-side textual comparison with clear
    section headers per persona.
    """
    lines: list[str] = []
    lines.append(f"Comparison for: {result.pr_url}")
    lines.append(f"Total time: {result.total_duration_ms}ms")
    lines.append("=" * 72)

    for entry in result.entries:
        lines.append("")
        if entry.error:
            lines.append(f"── {entry.persona_name} ── ERROR ({entry.duration_ms}ms)")
            lines.append(f"   {entry.error}")
            continue

        lines.append(f"── {entry.persona_name} ── verdict: {entry.result.verdict} ({entry.duration_ms}ms)")
        lines.append("")

        for section in entry.result.summary_sections:
            lines.append(f"  {section.emoji} {section.title}")
            for finding in section.findings:
                lines.append(f"    • {finding}")
            lines.append("")

        if entry.result.inline_comments:
            lines.append(f"  Inline comments ({len(entry.result.inline_comments)}):")
            for comment in entry.result.inline_comments:
                lines.append(f"    {comment.file}:{comment.line} — {comment.body}")
            lines.append("")

    return "\n".join(lines)


def format_comparison_api(result: ComparisonResult) -> dict:
    """Format a ComparisonResult as a JSON-serializable dict for API responses."""
    return {
        "pr_url": result.pr_url,
        "total_duration_ms": result.total_duration_ms,
        "entries": [
            {
                "persona_name": e.persona_name,
                "verdict": e.result.verdict,
                "duration_ms": e.duration_ms,
                "error": e.error,
                "summary_sections": [
                    {
                        "emoji": s.emoji,
                        "title": s.title,
                        "findings": s.findings,
                    }
                    for s in e.result.summary_sections
                ],
                "inline_comments": [
                    {"file": c.file, "line": c.line, "body": c.body}
                    for c in e.result.inline_comments
                ],
            }
            for e in result.entries
        ],
    }
```

#### Modified Files

**`review_bot/cli/main.py`**

Add the `compare` command to the CLI group:
```python
from review_bot.cli.compare_cmd import compare_cmd
cli.add_command(compare_cmd, name="compare")
```

**`review_bot/cli/compare_cmd.py`** (new file)

```python
from __future__ import annotations

import asyncio
import sys

import click


@click.command("compare")
@click.argument("pr_url")
@click.option(
    "--personas", "-p",
    required=True,
    help="Comma-separated list of persona names to compare",
)
@click.option(
    "--timeout", "-t",
    default=120.0,
    help="Timeout per persona review in seconds",
)
@click.option(
    "--json-output", "json_out",
    is_flag=True,
    help="Output as JSON instead of formatted text",
)
def compare_cmd(pr_url: str, personas: str, timeout: float, json_out: bool) -> None:
    """Compare how multiple personas would review a PR.

    Example: review-bot compare https://github.com/org/repo/pull/42 -p deepam,sarah
    """
    persona_names = [p.strip() for p in personas.split(",") if p.strip()]
    if len(persona_names) < 2:
        click.echo("Error: At least 2 persona names required for comparison", err=True)
        sys.exit(1)

    asyncio.run(_run_comparison(pr_url, persona_names, timeout, json_out))


async def _run_comparison(
    pr_url: str, persona_names: list[str], timeout: float, json_out: bool,
) -> None:
    import json
    import os
    import re
    import sys

    import httpx

    from review_bot.github.api import GitHubAPIClient
    from review_bot.persona.store import PersonaStore
    from review_bot.review.comparator import PersonaComparator
    from review_bot.review.comparison_formatter import (
        format_comparison_api,
        format_comparison_cli,
    )

    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not match:
        click.echo(f"Error: Invalid PR URL: {pr_url}", err=True)
        sys.exit(1)

    owner, repo, pr_number = match.group(1), match.group(2), int(match.group(3))

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        click.echo("Error: GITHUB_TOKEN or GH_TOKEN environment variable required", err=True)
        sys.exit(1)

    async with httpx.AsyncClient(
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
        },
    ) as http_client:
        github_client = GitHubAPIClient(http_client)
        persona_store = PersonaStore()
        comparator = PersonaComparator(github_client, persona_store)

        click.echo(f"Comparing {len(persona_names)} personas on {pr_url}...")
        result = await comparator.compare(
            owner, repo, pr_number, persona_names, timeout_per_persona=timeout,
        )

        if json_out:
            click.echo(json.dumps(format_comparison_api(result), indent=2))
        else:
            click.echo(format_comparison_cli(result))
```

**`review_bot/server/app.py`** (optional API endpoint)

Add a comparison API endpoint for programmatic access:

```python
# In a new router or added to existing
@router.post("/api/compare")
async def compare_personas(request: Request) -> dict:
    """Run persona comparison via API.

    Body: {"pr_url": "...", "personas": ["deepam", "sarah"]}
    """
    ...
```

### 3.3 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| **Persona doesn't exist** | `_review_with_persona` catches `FileNotFoundError` from `PersonaStore.load` and returns a `ComparisonEntry` with `error` set. The CLI/API output shows which personas failed and which succeeded. |
| **Extremely different review lengths** | The CLI formatter truncates individual findings to 200 characters with `...` suffix. The JSON API returns full text. The CLI adds summary stats (section count, comment count) per persona for quick scanning even when review bodies vary wildly. |
| **Timeout comparing 5+ personas** | `MAX_CONCURRENT_PERSONAS = 3` limits concurrent LLM calls. Each persona has an individual `timeout_per_persona` (default 120s). A comparison of 5 personas takes at most `ceil(5/3) * 120s = 240s`. The CLI shows a progress indicator per persona. Timed-out personas appear in results with an error message. |
| **Output format: CLI vs API** | Two separate formatters: `format_comparison_cli` produces ANSI-friendly text with dividers and indentation. `format_comparison_api` produces a flat JSON dict. The CLI `--json-output` flag switches between them. |
| **PR URL is invalid** | Validated in both CLI (`re.match`) and API endpoint. Returns a clear error message before any network calls. |
| **LLM returns inconsistent structures** | The existing `ReviewFormatter` handles this with JSON extraction fallback. Comparison inherits this resilience — each persona's result is independently formatted. |

### 3.4 Security Considerations

- CLI comparison uses the user's `GITHUB_TOKEN` / `GH_TOKEN` for API access
- API comparison endpoint should require authentication (same as dashboard token)
- No data is posted to GitHub — read-only operation
- LLM calls use the same auth as regular reviews (Claude CLI session)

### 3.5 Testing Approach

**`tests/test_comparator.py`**

- Mock `ClaudeReviewer.review` to return canned JSON for each persona
- Test 2-persona comparison returns both results
- Test persona-not-found returns entry with error
- Test timeout handling with a mock that sleeps longer than the timeout
- Test concurrent semaphore limits (mock with timing assertions)

**`tests/test_comparison_formatter.py`**

- Test `format_comparison_cli` output contains all persona names and verdicts
- Test `format_comparison_api` produces valid JSON-serializable dict
- Test error entries render correctly in both formats
- Test empty comparison (all personas failed)

**`tests/test_compare_cmd.py`**

- Test CLI with `click.testing.CliRunner`
- Test `--json-output` flag produces valid JSON
- Test invalid PR URL prints error and exits with code 1
- Test single persona name prints error (need at least 2)

---

## 4. Review Templates per Repo/Team

### 4.1 Config File Schema

**`.review-like-him.yml`** — placed in the repo root:

```yaml
# .review-like-him.yml
version: 1

# Default persona for this repo (overrides server default)
persona: deepam

# Minimum severity to post (low, medium, high, critical)
# Comments below this threshold are filtered out
min_severity: medium

# File patterns to skip (glob syntax)
skip_patterns:
  - "*.generated.ts"
  - "vendor/**"
  - "migrations/**"
  - "**/*.min.js"

# Additional instructions appended to the review prompt
custom_instructions: |
  This repo follows trunk-based development. PRs should be small.
  We use conventional commits. Check commit message format.
  Database queries must use parameterized inputs — flag any string concatenation.

# Per-persona overrides (optional)
persona_overrides:
  sarah:
    min_severity: low  # Sarah reviews more strictly in this repo
    custom_instructions: |
      Sarah is the security lead for this repo.
      Pay extra attention to auth and input validation.

# Maximum review comment count (avoid noisy reviews)
max_comments: 20
```

### 4.2 Pydantic Model

**`review_bot/config/repo_config.py`** (new file)

```python
from __future__ import annotations

import logging

import yaml
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("review-bot")

MAX_CONFIG_SIZE_BYTES: int = 64 * 1024  # 64 KB max config file size
SUPPORTED_VERSIONS: set[int] = {1}


class PersonaOverride(BaseModel):
    """Per-persona settings override for a specific repo."""
    min_severity: str | None = None
    custom_instructions: str | None = None
    skip_patterns: list[str] | None = None
    max_comments: int | None = None


class RepoConfig(BaseModel):
    """Per-repo review configuration loaded from .review-like-him.yml."""

    version: int = Field(default=1, description="Config schema version")
    persona: str | None = Field(default=None, description="Default persona for this repo")
    min_severity: str = Field(default="low", description="Minimum severity threshold")
    skip_patterns: list[str] = Field(default_factory=list, description="Glob patterns to skip")
    custom_instructions: str = Field(default="", description="Extra prompt instructions")
    persona_overrides: dict[str, PersonaOverride] = Field(
        default_factory=dict,
        description="Per-persona setting overrides",
    )
    max_comments: int = Field(default=50, ge=1, le=100, description="Max comments per review")

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: int) -> int:
        if v not in SUPPORTED_VERSIONS:
            raise ValueError(f"Unsupported config version {v}. Supported: {SUPPORTED_VERSIONS}")
        return v

    @field_validator("min_severity")
    @classmethod
    def _validate_severity(cls, v: str) -> str:
        valid = {"low", "medium", "high", "critical"}
        if v not in valid:
            raise ValueError(f"min_severity must be one of {valid}, got '{v}'")
        return v

    @classmethod
    def from_yaml(cls, yaml_str: str) -> RepoConfig:
        """Parse YAML string into a RepoConfig, with size and safety checks."""
        if len(yaml_str.encode("utf-8")) > MAX_CONFIG_SIZE_BYTES:
            raise ValueError(
                f"Config file exceeds maximum size of {MAX_CONFIG_SIZE_BYTES} bytes"
            )
        data = yaml.safe_load(yaml_str)
        if not isinstance(data, dict):
            raise ValueError("Config file must be a YAML mapping")
        return cls.model_validate(data)

    def resolve_for_persona(self, persona_name: str) -> RepoConfig:
        """Return a merged config with persona-specific overrides applied."""
        override = self.persona_overrides.get(persona_name)
        if not override:
            return self
        return self.model_copy(update={
            "min_severity": override.min_severity or self.min_severity,
            "custom_instructions": (
                self.custom_instructions + "\n" + override.custom_instructions
                if override.custom_instructions
                else self.custom_instructions
            ),
            "skip_patterns": override.skip_patterns or self.skip_patterns,
            "max_comments": override.max_comments or self.max_comments,
        })

    @classmethod
    def default(cls) -> RepoConfig:
        """Return the default config (used when no repo config is found)."""
        return cls()
```

### 4.3 File-by-File Changes

#### New Files

- `review_bot/config/repo_config.py` — as described above

#### Modified Files

**`review_bot/review/repo_scanner.py`**

Add config file loading to the `RepoScanner`:

```python
async def load_repo_config(self, owner: str, repo: str) -> RepoConfig:
    """Load .review-like-him.yml from the repo root via GitHub Contents API.

    Returns RepoConfig.default() if the file is not found or invalid.
    """
    from review_bot.config.repo_config import RepoConfig, MAX_CONFIG_SIZE_BYTES

    try:
        content = await self._read_file(owner, repo, ".review-like-him.yml")
        if content is None:
            logger.debug("No .review-like-him.yml found in %s/%s", owner, repo)
            return RepoConfig.default()
        return RepoConfig.from_yaml(content)
    except ValueError as exc:
        logger.warning(
            "Invalid .review-like-him.yml in %s/%s: %s — using defaults",
            owner, repo, exc,
        )
        return RepoConfig.default()
    except Exception as exc:
        logger.warning(
            "Failed to load .review-like-him.yml from %s/%s: %s — using defaults",
            owner, repo, exc,
        )
        return RepoConfig.default()
```

**`review_bot/review/orchestrator.py`**

Integrate repo config into the review pipeline:

1. After scanning repo context, load the repo config.
2. Resolve persona-specific overrides.
3. Filter files based on `skip_patterns`.
4. Pass `custom_instructions` to the prompt builder.
5. Filter output by `min_severity` before posting.
6. Truncate to `max_comments`.

```python
# In run_review(), between steps 3 and 4:
repo_config = await self._scanner.load_repo_config(owner, repo)
repo_config = repo_config.resolve_for_persona(persona_name)

# Filter files by skip_patterns
if repo_config.skip_patterns:
    files = self._filter_files(files, repo_config.skip_patterns)
    diff = self._filter_diff(diff, repo_config.skip_patterns)

# Step 4: Build prompt with custom instructions
prompt = self._prompt_builder.build(
    persona=persona,
    repo_context=repo_context,
    pr_data=pr_data,
    diff=diff,
    files=files,
    custom_instructions=repo_config.custom_instructions,  # new param
)

# After step 6 (format), filter by severity
result = self._apply_severity_filter(result, repo_config.min_severity)
result = self._apply_comment_limit(result, repo_config.max_comments)
```

**`review_bot/review/prompt_builder.py`**

Add `custom_instructions` parameter to `build()`:

```python
def build(
    self,
    persona: PersonaProfile,
    repo_context: RepoContext,
    pr_data: dict,
    diff: str,
    files: list,
    custom_instructions: str = "",  # new
) -> str:
    ...
    # Append custom instructions at the end of the system prompt
    if custom_instructions:
        prompt += f"\n\n## Additional Instructions\n{custom_instructions}"
    ...
```

### 4.4 Config Precedence Rules

Config is merged with this precedence (highest to lowest):

1. **Persona-specific override** in `.review-like-him.yml` (`persona_overrides.<name>`)
2. **Repo-level config** in `.review-like-him.yml` (top-level fields)
3. **Global server config** (`Settings` in `config/settings.py`)
4. **Hardcoded defaults** in `RepoConfig.default()`

For list fields (`skip_patterns`): persona override replaces (does not merge with) repo-level.
For string fields (`custom_instructions`): persona override appends to repo-level.
For scalar fields (`min_severity`, `max_comments`): persona override replaces repo-level.

### 4.5 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| **Malformed YAML** | `yaml.safe_load` raises `YAMLError`, caught in `load_repo_config`. Log warning with parse error details, fall back to `RepoConfig.default()`. |
| **Missing fields with defaults** | Pydantic `Field(default=...)` handles missing fields. All fields have sensible defaults. A file containing only `persona: deepam` is valid. |
| **Conflicting settings between repo and global** | Repo config always wins over global for fields it specifies. Global settings (`Settings`) provide the server-level defaults that repo config overrides. Document this clearly. |
| **File not found** | `_read_file` returns `None` for 404 HTTP responses (already handled). `load_repo_config` returns `RepoConfig.default()`. No error, no comment posted. |
| **Large config files** | `MAX_CONFIG_SIZE_BYTES = 64KB` enforced before parsing. The GitHub Contents API returns base64-encoded content, so the actual API response is ~33% larger. Check decoded size. |
| **YAML injection / security** | `yaml.safe_load` is used (never `yaml.load`), which prevents arbitrary Python object instantiation. Pydantic validation rejects unexpected types. `custom_instructions` is a plain string — it's injected into the LLM prompt as-is, which is safe because the LLM prompt is not executed as code. However, document that `custom_instructions` can influence review behavior (prompt injection via repo config is possible if the repo is untrusted). |
| **Version mismatch** | The `version` field validator rejects unknown versions with a clear error. This allows future schema evolution without silent breakage. |
| **Empty `skip_patterns` list** | No files are filtered. The `_filter_files` method short-circuits on empty patterns. |

### 4.6 Security Considerations

- **Prompt injection via `custom_instructions`**: A malicious repo owner could add instructions like "Approve everything." This is inherent to the design — the repo config is controlled by repo owners who also control the code being reviewed. Document this trust boundary: "Per-repo config is trusted to the same degree as the code in the repo."
- **YAML safety**: Always use `yaml.safe_load` — never `yaml.load`
- **File size**: Reject configs over 64KB to prevent memory issues
- **No secrets in config**: The config schema has no fields for tokens, keys, or credentials

### 4.7 Testing Approach

**`tests/test_repo_config.py`** (new)

- Test `RepoConfig.from_yaml` with a valid complete config
- Test missing fields use defaults
- Test invalid `min_severity` raises `ValueError`
- Test unsupported `version` raises `ValueError`
- Test `resolve_for_persona` merges override correctly
- Test `resolve_for_persona` with non-existent persona returns base config
- Test oversized YAML raises `ValueError`
- Test malformed YAML raises appropriate error
- Test empty YAML document (`None` from `yaml.safe_load`) raises `ValueError`
- Test `custom_instructions` append behavior in persona override

**`tests/test_repo_scanner.py`** (extend)

- Test `load_repo_config` with mocked GitHub Contents API returning valid YAML
- Test `load_repo_config` with 404 returns default config
- Test `load_repo_config` with malformed YAML returns default config

**`tests/test_orchestrator.py`** (extend)

- Test file filtering with `skip_patterns` (glob matching)
- Test `custom_instructions` appear in the prompt
- Test `min_severity` filtering removes low-severity comments

---

## 5. Slack/Discord Notifications

### 5.1 Notification Abstraction Layer

**`review_bot/notifications/__init__.py`**

```python
from __future__ import annotations
```

**`review_bot/notifications/base.py`** — Protocol/interface definition.

```python
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from review_bot.review.formatter import ReviewResult

logger = logging.getLogger("review-bot")


@runtime_checkable
class NotificationChannel(Protocol):
    """Protocol for notification delivery channels."""

    async def send(self, message: NotificationMessage) -> bool:
        """Send a notification. Returns True on success."""
        ...

    @property
    def channel_type(self) -> str:
        """Return the channel type identifier (e.g., 'slack', 'discord')."""
        ...


class NotificationMessage:
    """Structured notification content, channel-agnostic."""

    def __init__(
        self,
        *,
        title: str,
        pr_url: str,
        persona_name: str,
        repo: str,
        pr_number: int,
        verdict: str,
        summary: str,
        comment_count: int,
        success: bool = True,
        error_message: str | None = None,
    ) -> None:
        self.title = title
        self.pr_url = pr_url
        self.persona_name = persona_name
        self.repo = repo
        self.pr_number = pr_number
        self.verdict = verdict
        self.summary = summary
        self.comment_count = comment_count
        self.success = success
        self.error_message = error_message


class NotificationDispatcher:
    """Dispatches notifications to all configured channels."""

    def __init__(self, channels: list[NotificationChannel] | None = None) -> None:
        self._channels: list[NotificationChannel] = channels or []

    def add_channel(self, channel: NotificationChannel) -> None:
        self._channels.append(channel)

    async def notify(self, message: NotificationMessage) -> dict[str, bool]:
        """Send notification to all channels. Returns {channel_type: success}."""
        results: dict[str, bool] = {}
        for channel in self._channels:
            try:
                success = await channel.send(message)
                results[channel.channel_type] = success
            except Exception as exc:
                logger.error(
                    "Notification failed for %s: %s",
                    channel.channel_type, exc,
                )
                results[channel.channel_type] = False
        return results

    @staticmethod
    def build_message_from_result(
        result: ReviewResult,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> NotificationMessage:
        """Construct a NotificationMessage from a ReviewResult."""
        section_summary = ", ".join(
            f"{s.title}: {len(s.findings)}" for s in result.summary_sections
        )
        return NotificationMessage(
            title=f"Review posted on {owner}/{repo}#{pr_number}",
            pr_url=result.pr_url,
            persona_name=result.persona_name,
            repo=f"{owner}/{repo}",
            pr_number=pr_number,
            verdict=result.verdict,
            summary=section_summary or "No findings",
            comment_count=len(result.inline_comments),
        )
```

### 5.2 Slack Implementation

**`review_bot/notifications/slack.py`**

```python
from __future__ import annotations

import logging

import httpx

from review_bot.notifications.base import NotificationChannel, NotificationMessage

logger = logging.getLogger("review-bot")

SLACK_API_BASE = "https://slack.com/api"
MAX_MESSAGE_LENGTH = 3000  # Slack block text limit


class SlackNotifier:
    """Sends notifications via Slack Web API (chat.postMessage)."""

    def __init__(
        self,
        *,
        bot_token: str,
        channel: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = bot_token
        self._channel = channel
        self._client = http_client or httpx.AsyncClient()
        self._owns_client = http_client is None

    @property
    def channel_type(self) -> str:
        return "slack"

    async def send(self, message: NotificationMessage) -> bool:
        """Post a message to the configured Slack channel."""
        blocks = self._build_blocks(message)
        text_fallback = f"{message.title} — {message.verdict}"

        resp = await self._client.post(
            f"{SLACK_API_BASE}/chat.postMessage",
            headers={"Authorization": f"Bearer {self._token}"},
            json={
                "channel": self._channel,
                "text": text_fallback,
                "blocks": blocks,
                "unfurl_links": False,
            },
        )
        data = resp.json()
        if not data.get("ok"):
            error = data.get("error", "unknown")
            logger.error("Slack API error: %s", error)
            if error == "channel_not_found":
                logger.error("Slack channel '%s' not found — check config", self._channel)
            elif error == "invalid_auth":
                logger.error("Slack token is invalid or expired — rotate token")
            return False
        return True

    def _build_blocks(self, message: NotificationMessage) -> list[dict]:
        """Build Slack Block Kit blocks for the notification."""
        verdict_emoji = {
            "approve": "✅",
            "request_changes": "🔴",
            "comment": "💬",
        }.get(message.verdict, "📝")

        header = f"{verdict_emoji} *{message.persona_name}* reviewed <{message.pr_url}|{message.repo}#{message.pr_number}>"
        body = f"Verdict: *{message.verdict}*\n{message.summary}\nInline comments: {message.comment_count}"

        if message.error_message:
            body = f"⚠️ Review failed: {message.error_message}"

        # Truncate if too long
        if len(body) > MAX_MESSAGE_LENGTH:
            body = body[: MAX_MESSAGE_LENGTH - 3] + "..."

        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
            {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        ]

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
```

### 5.3 Discord Implementation

**`review_bot/notifications/discord.py`**

```python
from __future__ import annotations

import logging

import httpx

from review_bot.notifications.base import NotificationChannel, NotificationMessage

logger = logging.getLogger("review-bot")

MAX_EMBED_DESCRIPTION = 4096  # Discord embed description limit


class DiscordNotifier:
    """Sends notifications via Discord webhook."""

    def __init__(
        self,
        *,
        webhook_url: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._client = http_client or httpx.AsyncClient()
        self._owns_client = http_client is None

    @property
    def channel_type(self) -> str:
        return "discord"

    async def send(self, message: NotificationMessage) -> bool:
        """Post an embed to the configured Discord webhook."""
        embed = self._build_embed(message)

        resp = await self._client.post(
            self._webhook_url,
            json={"embeds": [embed]},
        )

        if resp.status_code == 404:
            logger.error("Discord webhook URL not found — check config")
            return False
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1)
            logger.warning("Discord rate limited, retry after %ss", retry_after)
            return False
        if resp.status_code >= 400:
            logger.error("Discord webhook error: %d %s", resp.status_code, resp.text)
            return False
        return True

    def _build_embed(self, message: NotificationMessage) -> dict:
        """Build a Discord embed for the notification."""
        color_map = {
            "approve": 0x2ECC71,       # green
            "request_changes": 0xE74C3C,  # red
            "comment": 0x3498DB,       # blue
        }

        description = f"**Verdict:** {message.verdict}\n{message.summary}\nInline comments: {message.comment_count}"
        if message.error_message:
            description = f"⚠️ Review failed: {message.error_message}"

        if len(description) > MAX_EMBED_DESCRIPTION:
            description = description[: MAX_EMBED_DESCRIPTION - 3] + "..."

        return {
            "title": f"{message.persona_name} reviewed {message.repo}#{message.pr_number}",
            "url": message.pr_url,
            "description": description,
            "color": color_map.get(message.verdict, 0x95A5A6),
        }

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
```

### 5.4 Configuration

**`review_bot/config/settings.py`** — Add notification settings:

```python
# New fields in Settings class
slack_bot_token: str = Field(default="", description="Slack bot OAuth token")
slack_channel: str = Field(default="", description="Slack channel ID for notifications")
discord_webhook_url: str = Field(default="", description="Discord webhook URL for notifications")
notify_on_success: bool = Field(default=True, description="Send notifications for successful reviews")
notify_on_failure: bool = Field(default=True, description="Send notifications for failed reviews")
```

**`review_bot/server/app.py`** — Initialize notification dispatcher in lifespan:

```python
from review_bot.notifications.base import NotificationDispatcher
from review_bot.notifications.slack import SlackNotifier
from review_bot.notifications.discord import DiscordNotifier

# In lifespan, after persona_store init:
dispatcher = NotificationDispatcher()
if app_settings.slack_bot_token and app_settings.slack_channel:
    dispatcher.add_channel(SlackNotifier(
        bot_token=app_settings.slack_bot_token,
        channel=app_settings.slack_channel,
    ))
if app_settings.discord_webhook_url:
    dispatcher.add_channel(DiscordNotifier(
        webhook_url=app_settings.discord_webhook_url,
    ))
app.state.notification_dispatcher = dispatcher
```

**`review_bot/server/queue.py`** — Send notifications after job completion:

```python
# After successful review in _process_job:
if hasattr(self, '_notification_dispatcher') and self._notification_dispatcher:
    message = NotificationDispatcher.build_message_from_result(result, job.owner, job.repo, job.pr_number)
    await self._notification_dispatcher.notify(message)

# After failed review:
if hasattr(self, '_notification_dispatcher') and self._notification_dispatcher:
    message = NotificationMessage(
        title=f"Review failed on {job.owner}/{job.repo}#{job.pr_number}",
        pr_url=f"https://github.com/{job.owner}/{job.repo}/pull/{job.pr_number}",
        persona_name=job.persona_name,
        repo=f"{job.owner}/{job.repo}",
        pr_number=job.pr_number,
        verdict="failed",
        summary="",
        comment_count=0,
        success=False,
        error_message=str(exc),
    )
    await self._notification_dispatcher.notify(message)
```

### 5.5 Per-Repo Webhook Configuration

Extend the `.review-like-him.yml` schema (from item 4) with notification overrides:

```yaml
notifications:
  slack:
    channel: "#frontend-reviews"  # Override global channel for this repo
  discord:
    webhook_url: "https://discord.com/api/webhooks/..."  # Repo-specific Discord
  on_success: true
  on_failure: true
```

This requires extending `RepoConfig` with a `notifications` field and passing it through the notification dispatcher.

### 5.6 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| **Notification delivery failure** | `NotificationDispatcher.notify` catches exceptions per channel and logs errors. Failed notifications do not block or retry — fire-and-forget. The review itself still succeeds. If retry is desired later, add a `notifications` table to the database and a background retry loop. |
| **Rate limiting on Slack/Discord** | Slack: Check `resp.json()["ok"]` and handle `ratelimited` error by logging and skipping. Discord: Check for 429 status and `retry_after` header. In v1, skip the notification on rate limit. Future: use the `retry_after` value with a delayed retry task. |
| **Message too long** | Slack: Truncate to `MAX_MESSAGE_LENGTH` (3000 chars) with `...`. Discord: Truncate embed description to 4096 chars. Both formatters truncate before sending. |
| **Channel not found** | Slack: `channel_not_found` error → log with specific message pointing to config. Discord: 404 response → log with config check suggestion. Neither crashes the server. |
| **Token rotation** | Slack bot tokens don't expire but can be revoked. Discord webhook URLs are permanent unless deleted. Log `invalid_auth` errors clearly so operators know to update the token. Settings are loaded at startup — token rotation requires restart (or future: config reload endpoint). |
| **Notification for failed vs successful reviews** | Controlled by `notify_on_success` and `notify_on_failure` settings. Default both True. Check before dispatching. Failed review notifications include the error message. |
| **No channels configured** | `NotificationDispatcher` with empty `_channels` list is a no-op. `notify()` returns an empty dict. No errors logged. |

### 5.7 Security Considerations

- **Slack bot token**: Stored in env var `REVIEW_BOT_SLACK_BOT_TOKEN`. Never logged. Never included in notification payloads.
- **Discord webhook URL**: Contains a secret token in the URL path. Stored in env var. Never logged in full (log only the domain).
- **Message content**: Notifications include repo name, PR number, persona name, verdict, and summary. No code snippets or diff content — prevents leaking proprietary code to notification channels.
- **Webhook URL validation**: Validate Discord webhook URLs match `https://discord.com/api/webhooks/...` pattern to prevent SSRF.

### 5.8 Dependencies

Add to `pyproject.toml`:
- No new dependencies — `httpx` (already a dependency) handles all HTTP calls to Slack and Discord APIs

### 5.9 Testing Approach

**`tests/test_notifications.py`** (new)

- Test `NotificationDispatcher` with mock channels (success and failure)
- Test `build_message_from_result` produces correct fields
- Test dispatcher continues when one channel fails

**`tests/test_slack_notifier.py`** (new)

- Mock `httpx.AsyncClient.post` to return Slack API responses
- Test successful send returns True
- Test `channel_not_found` error returns False
- Test `invalid_auth` error returns False
- Test message truncation at 3000 chars
- Test block structure matches Slack Block Kit format

**`tests/test_discord_notifier.py`** (new)

- Mock `httpx.AsyncClient.post` to return Discord responses
- Test successful send (204 response) returns True
- Test 404 (webhook not found) returns False
- Test 429 (rate limited) returns False
- Test embed structure and color mapping
- Test description truncation at 4096 chars

---

## 6. GitHub Actions Integration

### 6.1 Architecture Decision

**Recommendation: Docker action for v1, composite action as a lightweight alternative.**

| Option | Pros | Cons |
|--------|------|------|
| Docker action | Full environment control, reproducible, no host dependency issues | Slower startup (image pull), larger image size |
| Composite action | Fast startup, uses host Python, smaller footprint | Depends on host Python version, pip install on every run |

Ship both: Docker action as default (reliable), composite action as opt-in (fast).

### 6.2 Action File Structure

```
.github/actions/review-like-him/
├── action.yml           # Action metadata
├── Dockerfile           # Docker action image
├── entrypoint.sh        # Docker entrypoint
└── composite-action.yml # Alternative composite action
```

### 6.3 `action.yml` Schema

**`.github/actions/review-like-him/action.yml`**

```yaml
name: "Review Like Him"
description: "AI-powered code review mimicking real reviewer styles"
author: "review-like-him"

branding:
  icon: "eye"
  color: "purple"

inputs:
  persona:
    description: "Persona name to review as"
    required: true
  persona-path:
    description: "Path to persona YAML file (relative to repo root)"
    required: false
    default: ""
  persona-url:
    description: "URL to download persona YAML from a remote store"
    required: false
    default: ""
  github-token:
    description: "GitHub token for posting reviews (defaults to GITHUB_TOKEN)"
    required: false
    default: ${{ github.token }}
  anthropic-api-key:
    description: "Anthropic API key for Claude"
    required: true
  min-severity:
    description: "Minimum severity to post (low, medium, high, critical)"
    required: false
    default: "low"
  skip-patterns:
    description: "Comma-separated glob patterns for files to skip"
    required: false
    default: ""
  max-comments:
    description: "Maximum number of inline comments to post"
    required: false
    default: "50"
  fail-on-changes-requested:
    description: "Fail the action if the review requests changes"
    required: false
    default: "false"

outputs:
  verdict:
    description: "Review verdict: approve, request_changes, or comment"
  comment-count:
    description: "Number of inline comments posted"
  duration-ms:
    description: "Review duration in milliseconds"
  review-url:
    description: "URL of the posted review"

runs:
  using: "docker"
  image: "Dockerfile"
  env:
    INPUT_PERSONA: ${{ inputs.persona }}
    INPUT_PERSONA_PATH: ${{ inputs.persona-path }}
    INPUT_PERSONA_URL: ${{ inputs.persona-url }}
    INPUT_GITHUB_TOKEN: ${{ inputs.github-token }}
    INPUT_ANTHROPIC_API_KEY: ${{ inputs.anthropic-api-key }}
    INPUT_MIN_SEVERITY: ${{ inputs.min-severity }}
    INPUT_SKIP_PATTERNS: ${{ inputs.skip-patterns }}
    INPUT_MAX_COMMENTS: ${{ inputs.max-comments }}
    INPUT_FAIL_ON_CHANGES_REQUESTED: ${{ inputs.fail-on-changes-requested }}
```

### 6.4 Dockerfile

**`.github/actions/review-like-him/Dockerfile`**

```dockerfile
FROM python:3.11-slim

# Install review-bot
RUN pip install --no-cache-dir review-like-him

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
```

### 6.5 Entrypoint Script

**`.github/actions/review-like-him/entrypoint.sh`**

```bash
#!/bin/bash
set -euo pipefail

# Parse GitHub event to get PR info
PR_NUMBER=$(jq -r '.pull_request.number' "$GITHUB_EVENT_PATH")
REPO_FULL_NAME=$(jq -r '.repository.full_name' "$GITHUB_EVENT_PATH")

if [ "$PR_NUMBER" = "null" ] || [ -z "$PR_NUMBER" ]; then
    echo "::error::This action must run on pull_request events"
    exit 1
fi

PR_URL="https://github.com/${REPO_FULL_NAME}/pull/${PR_NUMBER}"

# Handle persona loading
PERSONA_DIR="$HOME/.review-bot/personas"
mkdir -p "$PERSONA_DIR"

if [ -n "$INPUT_PERSONA_PATH" ]; then
    # Load from repo file
    cp "$GITHUB_WORKSPACE/$INPUT_PERSONA_PATH" "$PERSONA_DIR/${INPUT_PERSONA}.yaml"
elif [ -n "$INPUT_PERSONA_URL" ]; then
    # Download from remote store
    curl -sSfL "$INPUT_PERSONA_URL" -o "$PERSONA_DIR/${INPUT_PERSONA}.yaml"
fi

# Set up environment
export GITHUB_TOKEN="$INPUT_GITHUB_TOKEN"
export ANTHROPIC_API_KEY="$INPUT_ANTHROPIC_API_KEY"

# Run review
OUTPUT=$(review-bot review "$PR_URL" \
    --as "$INPUT_PERSONA" \
    --min-severity "$INPUT_MIN_SEVERITY" \
    --max-comments "$INPUT_MAX_COMMENTS" \
    --json-output 2>&1) || true

# Parse output and set action outputs
VERDICT=$(echo "$OUTPUT" | jq -r '.verdict // "comment"')
COMMENT_COUNT=$(echo "$OUTPUT" | jq -r '.comment_count // "0"')
DURATION_MS=$(echo "$OUTPUT" | jq -r '.duration_ms // "0"')

echo "verdict=$VERDICT" >> "$GITHUB_OUTPUT"
echo "comment-count=$COMMENT_COUNT" >> "$GITHUB_OUTPUT"
echo "duration-ms=$DURATION_MS" >> "$GITHUB_OUTPUT"
echo "review-url=$PR_URL" >> "$GITHUB_OUTPUT"

# Optionally fail on changes requested
if [ "$INPUT_FAIL_ON_CHANGES_REQUESTED" = "true" ] && [ "$VERDICT" = "request_changes" ]; then
    echo "::error::Review requested changes — failing action as configured"
    exit 1
fi
```

### 6.6 Workflow Example

```yaml
# .github/workflows/review-like-him.yml
name: AI Code Review

on:
  pull_request:
    types: [opened, synchronize]

permissions:
  pull-requests: write
  contents: read

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Cache persona profiles
        uses: actions/cache@v4
        with:
          path: ~/.review-bot/personas
          key: persona-${{ hashFiles('.personas/**') }}

      - name: Run AI Review
        id: review
        uses: your-org/review-like-him@v1
        with:
          persona: deepam
          persona-path: .personas/deepam.yaml
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          min-severity: medium
          fail-on-changes-requested: false

      - name: Review Summary
        if: always()
        run: |
          echo "Verdict: ${{ steps.review.outputs.verdict }}"
          echo "Comments: ${{ steps.review.outputs.comment-count }}"
          echo "Duration: ${{ steps.review.outputs.duration-ms }}ms"
```

### 6.7 File-by-File Changes

#### New Files

- `.github/actions/review-like-him/action.yml` — Action metadata
- `.github/actions/review-like-him/Dockerfile` — Docker image
- `.github/actions/review-like-him/entrypoint.sh` — Entrypoint script

#### Modified Files

**`review_bot/cli/review_cmd.py`**

Add `--json-output` flag to output structured JSON (needed for the action to parse results):

```python
@click.option("--json-output", is_flag=True, help="Output structured JSON")
@click.option("--min-severity", default="low", help="Minimum severity threshold")
@click.option("--max-comments", default=50, type=int, help="Maximum inline comments")
```

**`pyproject.toml`**

Add the action files to package data and ensure the CLI entry point supports the new flags.

### 6.8 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| **Action timeout** | GitHub Actions has a default 6-hour timeout. Set a job-level timeout: `timeout-minutes: 10` in the workflow. The entrypoint wraps the review command with a timeout. If the LLM is slow, the action fails gracefully with a timeout error. |
| **Secrets management** | `ANTHROPIC_API_KEY` is passed as a secret input and set as an env var — never logged. The `GITHUB_TOKEN` is the built-in Actions token with `pull-requests: write` permission. Never use `${{ secrets.GITHUB_TOKEN }}` directly in logs. The Dockerfile uses `--no-cache-dir` to avoid caching credentials. |
| **Large PR diff exceeding action memory** | GitHub-hosted runners have 7GB RAM. A very large diff (10k+ files) could exhaust memory during LLM processing. The existing `LARGE_PR_FILE_THRESHOLD` (500 files) in the orchestrator handles this by posting a summary-only comment. The entrypoint also limits diff size by checking `$(wc -c < diff)` before processing. |
| **Caching persona profiles across runs** | Use `actions/cache@v4` with a key based on the persona YAML file hash. The workflow example above shows this pattern. Persona files are small (~5KB) so cache impact is minimal. Cache invalidation happens automatically when the persona YAML changes. |
| **Versioning the action** | Use semantic versioning with git tags: `v1`, `v1.0.0`, `v1.1.0`. The `v1` tag points to the latest `v1.x.x` release (GitHub Actions convention). Breaking changes bump the major version. The Dockerfile pins the `review-like-him` package version via `pip install review-like-him==X.Y.Z`. |
| **Self-hosted runners compatibility** | Docker actions work on Linux self-hosted runners out of the box. For macOS/Windows runners, the composite action alternative uses the host's Python. Document runner requirements: Linux (Docker action) or Python 3.11+ (composite action). Test both in CI. |
| **Persona loading priority** | 1. `persona-path` (file in repo), 2. `persona-url` (remote download), 3. Cached profile from previous run, 4. Error if none found. The entrypoint checks each source in order. |
| **Fork PRs** | Actions from forks have read-only `GITHUB_TOKEN` by default — reviews can't be posted. Document this limitation: "For fork PRs, the action runs but cannot post reviews. Use `pull_request_target` event instead (with appropriate security considerations)." |

### 6.9 Security Considerations

- **Secret exposure**: Anthropic API key is a required secret — never print it in logs. Use GitHub's built-in secret masking.
- **`pull_request_target` risks**: If using `pull_request_target` for fork PRs, the action runs with write access to the base repo. Document that `pull_request_target` should only be used with trusted review content (the persona YAML and review output, not arbitrary fork code).
- **Docker image supply chain**: Pin base image digests in production Dockerfile. Use multi-stage builds to minimize attack surface.
- **No shell injection**: The entrypoint uses `"$VAR"` quoting consistently. PR numbers are validated as integers via `jq`.

### 6.10 Deployment Notes

- Publish the action to the GitHub Marketplace or as a private action in the org
- The Docker image can be pre-built and published to GHCR for faster startup:
  ```yaml
  runs:
    using: "docker"
    image: "docker://ghcr.io/your-org/review-like-him-action:v1"
  ```
- Version the action independently from the Python package (action `v1` may use package `v0.2.0`)

### 6.11 Testing Approach

**`tests/test_action_entrypoint.sh`** (bash tests)

- Test entrypoint with mock `GITHUB_EVENT_PATH` JSON files
- Test PR number extraction from event payload
- Test persona loading from file path
- Test persona loading from URL (mock curl)
- Test `fail-on-changes-requested` exit code behavior
- Test invalid event (missing PR number) produces error

**`tests/test_review_cmd.py`** (extend existing)

- Test `--json-output` produces valid JSON with expected fields
- Test `--min-severity` filtering
- Test `--max-comments` truncation

**Integration test (CI workflow)**

- Add a `.github/workflows/test-action.yml` that runs the action against a known test PR
- Use a test persona with deterministic output (mock LLM)
- Verify the action posts a review and sets correct outputs

---

## Cross-Cutting Concerns

### Dependency Summary

| Feature | New Dependencies |
|---------|-----------------|
| Team Dashboard | `jinja2` (templates) |
| Multiple Persona Assignment | None |
| Persona Comparison | None |
| Review Templates | None |
| Slack/Discord Notifications | None (uses existing `httpx`) |
| GitHub Actions | None (standalone Docker image) |

### Database Schema Additions

```sql
-- Deduplication index for multiple persona assignment (item 2)
CREATE INDEX IF NOT EXISTS idx_jobs_dedup
ON jobs(owner, repo, pr_number, persona_name, status);

-- Notification log (item 5, optional)
CREATE TABLE IF NOT EXISTS notification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER,
    channel_type TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    sent_at TEXT NOT NULL,
    FOREIGN KEY (review_id) REFERENCES reviews(id)
);
```

### Implementation Order

Recommended build sequence (dependencies flow downward):

```
1. Review Templates (item 4)     — foundational, no dependencies
2. Multiple Persona Assignment (item 2) — extends webhook handler
3. Persona Comparison (item 3)   — reuses review pipeline
4. Slack/Discord Notifications (item 5) — hooks into queue worker
5. Team Dashboard (item 6.1)     — reads from database populated by 1-4
6. GitHub Actions (item 6)       — packages CLI, independent of server features
```

Items 4 and 6 are fully independent and can be developed in parallel. Items 2 and 5 have a light dependency (notifications fire after multi-persona reviews). Item 1 (dashboard) benefits from having more data in the database from items 2-4.

### Migration Path

All changes are additive — no breaking changes to existing APIs, CLI commands, or database schema. Features degrade gracefully when not configured:

- No `.review-like-him.yml` → default settings
- No notification channels configured → no-op dispatcher
- No `--personas` flag → single persona behavior unchanged
- Dashboard routes return empty states on fresh installations

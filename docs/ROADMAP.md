# review-like-him — Roadmap

> Last updated: 2026-03-15

This document outlines planned improvements for review-like-him, organized into phases by priority. Each item includes a brief description, motivation, and rough complexity estimate.

**Complexity key:** 🟢 Small (< 1 day) · 🟡 Medium (1–3 days) · 🔴 Large (3+ days)

---

## Phase 1 — Reliability & Polish

Foundation work to make the existing pipeline production-ready and operationally sound.

### Incremental persona updates
🟡 Medium

Only mine new reviews since the last update instead of re-fetching the entire history. The miner currently uses GitHub Search API with full pagination on every run — adding a `last_mined_at` timestamp to persona YAML and passing `created:>TIMESTAMP` to the search query would skip already-processed reviews. Matters because full re-mines are slow (rate-limited to ~5k requests/hour) and wasteful for active personas that get updated frequently.

### Comment deduplication in mining
🟡 Medium

Distinguish thread replies from standalone review comments during mining. Right now `get_pull_request_comments` fetches all inline comments without resolving conversation threads, so a reply like "good point, fixed" gets the same weight as the original substantive comment. Deduplication should collapse reply chains and only weight the reviewer's original observations, improving persona accuracy.

### PostgreSQL migration path
🔴 Large

Replace the async SQLite backend (`aiosqlite`) with PostgreSQL via `asyncpg` for team deployments. The current schema (reviews, jobs, persona_stats tables) is simple enough that the migration is straightforward, but it requires: a configurable `DATABASE_URL` in settings, connection pool tuning, and tested deployment instructions. Teams running multiple instances need a shared database — SQLite's write lock makes concurrent workers unreliable.

### Health check endpoint
🟢 Small

Add `GET /health` returning database connectivity, queue depth, worker status, and GitHub API rate limit remaining. The FastAPI app currently has no observability endpoints. Kubernetes liveness/readiness probes, uptime monitors, and load balancers all need a health endpoint to function. Should also expose the installed GitHub App's connection status.

### Graceful shutdown with job drain
🟡 Medium

Handle `SIGTERM`/`SIGINT` by stopping the queue worker from accepting new jobs, waiting for in-flight reviews to complete (with a configurable timeout), then disposing database connections. The current shutdown cancels the worker task immediately via `asyncio.CancelledError` — a review mid-post could leave a partial comment on a PR. Add a drain period (default 30s) before force-killing.

### Rate limit dashboard / status
🟡 Medium

Track and expose GitHub API rate limit consumption across all endpoints. The API client already handles 429 responses with backoff, but there's no visibility into how close the app is to its limits. Add `X-RateLimit-Remaining` and `X-RateLimit-Reset` header parsing to `GitHubAPIClient`, store the values, and expose them via a `GET /status/rate-limits` endpoint and a `review-bot status` CLI command.

---

## Phase 2 — Smarter Reviews

Improvements to review quality, relevance, and intelligence.

### Multi-pass review for large PRs
🔴 Large

Split large diffs into logical chunks (by directory, file type, or size), review each chunk independently, then merge and deduplicate findings. The orchestrator currently sends the entire diff to Claude in one shot — PRs over ~500 files get a summary-only comment instead. Multi-pass would: (1) partition the diff, (2) review each partition with shared repo context, (3) merge overlapping comments, (4) rank by severity. Requires careful prompt engineering to maintain cross-chunk awareness.

### Context-aware reviews
🔴 Large

Go beyond convention detection to understand repository architecture — module boundaries, data flow patterns, API contracts, and ownership. The `RepoScanner` currently detects languages, frameworks, linters, and CI systems but doesn't understand _how_ the codebase is structured. Adding architecture awareness (e.g., "this is a controller, it shouldn't contain business logic") would produce reviews that match how experienced developers think about a codebase. Could leverage repo README, directory structure heuristics, and import graph analysis.

### Learning from review feedback
🔴 Large

Let PR authors react to bot comments (👍/👎 or resolve/dismiss) and feed that signal back into persona refinement. Currently there's no feedback loop — the persona is static after mining. Tracking which comments get positive reactions vs. which get dismissed would allow re-weighting the persona's priorities over time. Requires: webhook handling for comment reactions, a feedback table in the database, and periodic persona re-analysis incorporating feedback scores.

### Confidence scores on comments
🟡 Medium

Ask the LLM to rate its confidence (high/medium/low) on each review comment based on how clearly the issue violates the persona's known preferences. Surface this in the formatted output (e.g., as emoji markers or a severity prefix). Helps reviewers triage bot comments — high-confidence comments likely match real review patterns, while low-confidence ones are more speculative. Implementation is mostly prompt engineering plus formatter changes.

### Severity-based filtering
🟡 Medium

Only post comments above a configurable severity threshold per repository. Busy repos with high PR volume don't want 15 nitpicks on every PR — they want the 2–3 critical issues. Add a `min_severity` setting (per repo or global) and filter the LLM's output before posting. The prompt already asks for categorized feedback; this adds a gate between the reviewer and the poster.

### File-type-aware review strategies
🟡 Medium

Apply different review depth and focus areas based on file type. Database migrations should be reviewed for safety (missing transactions, destructive operations, index locks) while business logic gets architecture and correctness scrutiny. Test files get coverage and assertion quality checks. The `RepoScanner` already detects file types — this extends the `PromptBuilder` to include file-type-specific instructions in the review prompt.

---

## Phase 3 — Team Features

Collaborative features for teams using review-like-him across multiple repos and reviewers.

### Team dashboard (web UI)
🔴 Large

A web interface showing review activity, persona accuracy trends, queue status, and configuration. Built as a separate FastAPI route group (or lightweight React SPA) served from the existing server. Displays: reviews per persona over time, average comment count, feedback scores (once Phase 2 learning lands), and active job status. The database already stores review logs with timestamps and metrics — this surfaces that data visually.

### Multiple persona assignment
🟡 Medium

Configure a repo to receive reviews from 2+ personas on the same PR, each posting independently. Useful for getting both a senior architect's perspective and a testing-focused reviewer's perspective. Implementation: extend the webhook handler to fan out a single PR event into multiple queued jobs (one per assigned persona), with deduplication to avoid re-reviewing if the same persona is assigned twice.

### Persona comparison
🟡 Medium

A CLI command or API endpoint that runs a diff through multiple personas and shows how each would review it, side by side. Useful for persona calibration ("does this persona actually sound like the person?") and for understanding stylistic differences across team members. Reuses the existing review pipeline but skips the posting step, returning structured results for comparison.

### Review templates per repo/team
🟢 Small

Allow repos to include a `.review-like-him.yml` config file specifying: which persona to use, minimum severity, file patterns to skip, and custom instructions to append to the review prompt. Currently all configuration is global — per-repo templates let teams customize without changing the server config. The orchestrator would check for this file via the GitHub Contents API during repo scanning.

### Slack / Discord notifications
🟡 Medium

Send a message to a configured channel when a review is posted, including a summary and link to the PR. Useful for teams that don't rely on GitHub notification emails. Requires: a notification abstraction layer (to support Slack, Discord, and future integrations), webhook URL configuration per repo/team, and a message formatter. Use the Slack Web API (`chat.postMessage`) and Discord webhooks.

### GitHub Actions integration
🟡 Medium

Publish a GitHub Action that runs review-like-him as a CI step instead of requiring a persistent webhook server. The action would: install the CLI, load a persona from the repo or a remote store, run `review-bot review` on the PR diff, and post results. Lowers the barrier to adoption — teams can try it without deploying infrastructure. Requires packaging the CLI as a Docker action or composite action.

---

## Phase 4 — Platform

Expanding beyond GitHub and Claude to become a general-purpose review platform.

### GitLab / Bitbucket support
🔴 Large

Abstract the GitHub-specific code (`github/api.py`, `github/app.py`, `github/setup.py`) behind a platform interface, then implement GitLab and Bitbucket backends. The mining, review, and posting logic would remain the same — only the API layer changes. GitLab uses a different auth model (project access tokens vs GitHub Apps) and different webhook payloads, so the abstraction needs to be carefully designed. Start with GitLab (larger market) then Bitbucket.

### Custom LLM backends
🟡 Medium

Support OpenAI, Anthropic direct API, and local models (Ollama, vLLM) as alternatives to Claude Agent SDK. The `ClaudeReviewer` class is the only LLM touchpoint — wrap it in a `ReviewerBackend` protocol with implementations for each provider. Local models enable air-gapped deployments; OpenAI support broadens adoption. Each backend needs its own prompt tuning since models respond differently to the same instructions.

### Persona marketplace
🔴 Large

A hosted registry where teams can publish anonymized review styles (e.g., "Security-focused senior engineer" or "Performance-obsessed backend dev") for others to use. Personas would be stripped of identifying information and published as YAML profiles. Requires: a registry API, persona anonymization pipeline, search/discovery, and versioning. Privacy is the hardest part — ensuring published personas can't be de-anonymized from their review patterns.

### Programmatic API
🟡 Medium

A documented REST API for triggering reviews, managing personas, and querying review history without the CLI or webhooks. The FastAPI server already handles webhooks — this extends it with authenticated CRUD endpoints (`POST /api/reviews`, `GET /api/personas`, etc.). Enables integration with custom dashboards, CI pipelines, and third-party tools. Add API key auth and OpenAPI documentation.

### Plugin system for custom review rules
🔴 Large

Allow users to write Python plugins that add custom review rules (e.g., "flag any SQL query without parameterized inputs" or "require error handling in all API endpoints"). Plugins would hook into the review pipeline between scanning and prompting, adding extra context or constraints to the LLM prompt. Requires: a plugin discovery mechanism, a stable hook API, sandboxing for untrusted plugins, and documentation.

### Self-hosted deployment guide
🟡 Medium

Production-ready Docker images and Kubernetes manifests with: multi-stage Dockerfile, Helm chart, health check probes, resource limits, secrets management (GitHub App private key, API keys), persistent volume for SQLite or PostgreSQL connection config, and horizontal scaling guidance. The current `setup.sh` handles local development — this covers production deployments. Include docker-compose for small teams and Helm for larger organizations.

---

## Technical Debt

Cross-cutting improvements to code quality, observability, and developer experience.

### Database migration framework (Alembic)
🟡 Medium

Replace raw `CREATE TABLE IF NOT EXISTS` SQL in `create_app()` with Alembic migrations. The current approach has no version tracking — schema changes require manual intervention or data loss. Alembic provides: versioned migration scripts, rollback support, and auto-generation from SQLAlchemy models. Essential before any schema changes (PostgreSQL migration, feedback tables, etc.) land.

### Structured logging (JSON format)
🟡 Medium

Switch from the current `StreamHandler` with text formatting to JSON-structured logs via `structlog` or `python-json-logger`. The existing `setup_logging()` produces human-readable but unparseable logs. Structured logging enables: log aggregation (Datadog, ELK, CloudWatch), filtering by field (request_id, persona, pr_url), and correlation across async tasks. Add context propagation so all log entries from a single review job share a job ID.

### OpenTelemetry tracing
🟡 Medium

Instrument the review pipeline with OpenTelemetry spans: mining duration, LLM latency, GitHub API call counts, queue wait time. The codebase currently logs timing only for the overall review (`duration_ms` in the reviews table) but has no visibility into where time is spent within a review. Tracing would reveal bottlenecks (e.g., "90% of review time is waiting for Claude") and enable distributed tracing across services.

### CI/CD pipeline
🟢 Small

Add GitHub Actions workflows for: running `pytest` on PRs, `ruff` linting, type checking with `mypy`, and publishing to PyPI on tagged releases. The project has `pytest` and `ruff` as dev dependencies but no CI configuration — contributions could break tests without anyone knowing. Start with a simple `.github/workflows/ci.yml` running tests on Python 3.11+ across platforms.

### Performance benchmarks
🟡 Medium

Establish baseline metrics for: persona mining throughput (reviews/minute), review latency (prompt-to-post), queue throughput (jobs/minute), and memory usage under load. Without benchmarks, performance regressions go unnoticed. Add a `benchmarks/` directory with reproducible scripts using synthetic data (mock GitHub API responses, fixture diffs) and record results in CI for trend tracking.

---

## Contributing

If you're interested in contributing to any of these items, open an issue referencing the roadmap item to discuss the approach before starting work. Items marked 🟢 are good first contributions.

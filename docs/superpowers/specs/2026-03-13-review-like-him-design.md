# review-like-him — Design Specification

**Date:** 2026-03-13
**Status:** Approved
**Project:** `/Users/mtarun/Desktop/SideHustles/review-like-him`

## Summary

A GitHub App that creates AI-powered reviewer bots mimicking real people's code review style. Assign `deepam-bot` to a PR and get a review as if Deepam wrote it — categorized feedback, inline comments, in their voice and priorities. Supports multiple persona bots reviewing the same PR independently.

## Problem

When a key reviewer (e.g., a tech lead or senior engineer) is unavailable — on leave, in a different timezone, overloaded — PRs either wait or get merged without their perspective. Teams lose the benefit of that person's domain knowledge, code standards enforcement, and architectural oversight.

## Solution

Build a CLI tool + GitHub App that:
1. Mines a reviewer's past PR review history across repos to learn their style
2. Creates a bot GitHub account that can be assigned as a PR reviewer
3. Uses Claude Code SDK (login-based, no API keys) to review code through that person's lens
4. Posts rich, categorized reviews with inline comments in the person's voice
5. Adapts to each repo's conventions (no demanding tests in repos without test infrastructure)

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Persona learning | Hybrid: mine past reviews + manual overrides | Accurate baseline from real data, customizable |
| Cross-repo personas | Yes — mine across all accessible repos | Richer persona, portable across projects |
| Temporal weighting | Yes — recent reviews weighted higher | Captures current review style, not outdated habits |
| GitHub integration | GitHub App | Real bot accounts, native reviewer assignment UX |
| LLM engine | Claude Code SDK (CLI login auth) | No API keys needed, uses existing Claude subscription |
| Multi-bot behavior | Independent reviews per bot | Overlap is signal, not noise. Scales cleanly |
| Review format | Rich summary (categorized sections) + inline comments | Persona voice in categories (bugs, arch, style), plus line-level comments |
| Tech stack | Python + FastAPI + SQLite + Claude Code SDK | LLM-native ecosystem, async webhooks, zero-config DB |
| Architecture | Monolith (designed for later distribution) | Simple setup, clean module boundaries allow Redis/worker swap later |
| UI | CLI-first | Zero friction: pip install + 3 commands = running |
| Persona permissions | Anyone with repo access can create any persona | The person being modeled doesn't need to be involved |
| Repo awareness | Auto-detected per review | Bot scans repo conventions before reviewing, adapts persona accordingly |

## User Experience

### Setup (one-time)

```bash
pip install review-like-him

review-bot init
# → Walks through GitHub App creation (opens browser with pre-filled settings)
# → Configures webhook URL (auto-detects ngrok for local, or asks for remote URL)
# → Verifies claude CLI is logged in
# → Saves config to ~/.review-bot/config.yaml
# → "✓ Setup complete."

review-bot persona create deepam --github-user deepam-actual
# → Mines review history via GitHub API (all accessible repos)
# → Applies temporal weighting (recent reviews matter more)
# → Runs LLM analysis to extract style/priorities/tone
# → Shows preview: "Here's what I learned about Deepam..."
# → Optional tweaks
# → Saves to ~/.review-bot/personas/deepam.yaml

review-bot start
# → "✓ Listening. Assign deepam-bot on any PR to trigger."
```

### Daily workflow

1. Developer opens a PR
2. Assigns `deepam-bot` as reviewer (appears in GitHub's reviewer dropdown)
3. Bot picks up the webhook event
4. Claude reviews the code using Deepam's persona + repo context
5. Bot posts a rich categorized review + inline comments
6. Developer fixes issues, re-requests review — bot reviews again

### Full CLI command set

```
review-bot init                              # Interactive setup wizard
review-bot persona create <name> --github-user <user>  # Create persona from GitHub history
review-bot persona list                      # List all personas with stats
review-bot persona show <name>               # Display full persona profile
review-bot persona update <name>             # Re-mine latest reviews (incremental)
review-bot persona edit <name>               # Open persona YAML in $EDITOR
review-bot start                             # Start webhook listener (foreground)
review-bot start --daemon                    # Start as background service
review-bot status                            # Show running state, active personas, recent reviews
review-bot logs                              # Tail recent review activity
review-bot review <pr-url> --as <name>       # Manual trigger for testing
```

## Architecture

### System overview

```
GitHub (webhook: review_requested)
    │
    ▼
FastAPI Webhook Receiver (signature validation, persona mapping, job queuing)
    │
    ▼
Review Orchestrator
    ├── Load persona profile (YAML)
    ├── Fetch PR data (diff, description, files)
    ├── Scan repo context (tests? CI? linting? framework?)
    ├── Build prompt (persona + repo context + diff)
    ├── Execute review (Claude Code SDK)
    ├── Format output (categorized summary + inline comments)
    └── Post review (GitHub API)
    │
    ▼
GitHub (review posted on PR)
```

### Key components

**Webhook Receiver** (`server/`): FastAPI endpoint that validates GitHub HMAC signatures, maps bot usernames to persona names, and queues review jobs. Lightweight intake only.

**Persona Engine** (`persona/`): Mines GitHub review history via API, applies temporal weighting (last 3 months: 3x, 3-12 months: 1.5x, 12+ months: 0.5x), runs LLM analysis to extract structured persona profiles. Stores as YAML files.

**Review Orchestrator** (`review/`): The core flow. Loads persona, fetches PR data, scans repo conventions, builds a combined prompt, calls Claude Code SDK, parses output into structured sections, and posts the review.

**Repo Scanner** (`review/repo_scanner.py`): Auto-detects repo conventions before each review. Checks for test infrastructure, CI config, linting setup, language/framework patterns. Ensures the persona adapts to repo context — won't demand tests in repos that have none.

**GitHub Integration** (`github/`): GitHub App authentication (JWT + installation tokens), API client for PRs/reviews/comments, and an interactive setup helper that guides users through App creation.

### Data stores

- **Persona profiles:** `~/.review-bot/personas/<name>.yaml` — human-readable, editable
- **Config:** `~/.review-bot/config.yaml` — GitHub App credentials, webhook URL, settings
- **Job history + state:** SQLite at `~/.review-bot/review-bot.db` — review history, job queue, analytics
- **Repo cache:** `~/.review-bot/repos/` — cached clones for faster diff access

### Scaling path

v1 is a monolith (single FastAPI process, asyncio job queue, SQLite). Module boundaries are designed so that swapping to Redis queue + worker pool + PostgreSQL is a config change, not a rewrite:

- `server/queue.py` abstracts job queuing (asyncio queue → Redis)
- `config/settings.py` supports both SQLite and PostgreSQL connection strings
- Workers are just the orchestrator running in a loop — same code, different deployment

## Persona Engine — Detail

### Mining pipeline

```
GitHub API (paginated)
    → Fetch all review comments by user across accessible repos
    → Fetch review verdicts (approve, request changes, comment)
    → Fetch review threads (to understand what they engage with)
    ↓
Temporal weighting
    → Last 3 months: weight 3x
    → 3-12 months: weight 1.5x
    → 12+ months: weight 0.5x
    ↓
LLM analysis (Claude)
    → "Analyze these N comments. What does this person care about?"
    → Extract: priorities, pet peeves, tone, severity patterns, approval criteria
    ↓
Persona profile (YAML)
```

### Persona profile schema

```yaml
name: deepam
github_user: deepam-actual
mined_from: 847 comments across 12 repos
last_updated: 2026-03-13

priorities:
  - category: error_handling
    severity: critical
    description: "Always flags missing error paths"
  - category: test_coverage
    severity: strict
    description: "Won't approve without tests for new logic"
  - category: naming
    severity: moderate
    description: "Prefers descriptive over short names"
  - category: architecture
    severity: opinionated
    description: "Hates god classes, pushes for SRP"

pet_peeves:
  - "magic numbers without constants"
  - "catch-all exception handlers"
  - "commented-out code left in PRs"

tone: "direct but supportive. Uses humor. Explains the 'why' behind feedback."

severity_pattern:
  blocks_on:
    - "missing error handling"
    - "no tests for new logic"
    - "security issues"
  nits_on:
    - "naming conventions"
    - "formatting"
    - "minor style issues"
  approves_when: "logic is sound, tests exist, errors are handled"

overrides:
  - "Extra strict about database migrations"
  - "Prefers composition over inheritance, always"
```

### Repo-aware adaptation

Before every review, the repo scanner checks:
- Test directory / test files / test framework config
- CI configuration (GitHub Actions, etc.)
- Linting / formatting config
- Language and framework detection
- Patterns in merged PR history

The review prompt includes repo context so the persona adapts. A reviewer who normally demands tests will not flag missing tests in a repo with no test infrastructure. The bot focuses on what the reviewer WOULD flag in that specific repo context.

## Review Output — Detail

### Top-level review body (posted as PR review)

Categorized sections, each populated based on what the persona cares about + what the AI catches independently:

- 🐛 **Bugs** — logic errors, race conditions, edge cases
- 🏗️ **Architecture** — structural concerns, SRP violations, coupling
- 🧪 **Testing** — missing coverage, weak assertions (only if repo has tests)
- 💅 **Style** — naming, formatting, persona-specific pet peeves
- 🔒 **Security** — vulnerabilities, injection risks, auth issues
- ⚡ **Performance** — inefficiencies, N+1 queries, unnecessary allocations

Sections only appear if there are findings. Empty categories are omitted.

The tone and phrasing matches the persona. "Magic number on line 42. Extract to a named constant — you know how I feel about these 😄" vs a generic "Consider extracting magic number to constant."

### Inline comments

Posted on specific lines in the PR diff via GitHub's review comment API. Each comment includes:
- The specific issue
- Why it matters (in the persona's voice)
- Suggested fix when applicable

### Review verdict

The bot submits one of:
- **Approve** — if the persona would approve based on their approval criteria
- **Request changes** — if blocking issues are found (per the persona's `blocks_on` list)
- **Comment** — if only nits/suggestions, no blockers

## Project Structure

```
review-like-him/
├── pyproject.toml
├── README.md
├── LICENSE
│
├── review_bot/
│   ├── __init__.py
│   ├── cli/
│   │   ├── __init__.py
│   │   ├── main.py              # Click command group, entry point
│   │   ├── init_cmd.py          # review-bot init
│   │   ├── persona_cmd.py       # review-bot persona *
│   │   ├── server_cmd.py        # review-bot start/status/logs
│   │   └── review_cmd.py        # review-bot review (manual trigger)
│   │
│   ├── server/
│   │   ├── __init__.py
│   │   ├── app.py               # FastAPI app factory
│   │   ├── webhooks.py          # GitHub webhook endpoint + HMAC validation
│   │   └── queue.py             # Async job queue (asyncio, upgradeable to Redis)
│   │
│   ├── persona/
│   │   ├── __init__.py
│   │   ├── miner.py             # GitHub review history mining
│   │   ├── analyzer.py          # LLM analysis → persona profile
│   │   ├── profile.py           # Pydantic model + YAML serialization
│   │   ├── temporal.py          # Temporal weighting logic
│   │   └── store.py             # CRUD for ~/.review-bot/personas/
│   │
│   ├── review/
│   │   ├── __init__.py
│   │   ├── orchestrator.py      # Main review flow
│   │   ├── repo_scanner.py      # Auto-detect repo conventions
│   │   ├── prompt_builder.py    # Build system prompt (persona + context + diff)
│   │   ├── reviewer.py          # Claude Code SDK execution wrapper
│   │   ├── formatter.py         # Parse LLM output → structured review
│   │   └── github_poster.py     # Post review + inline comments via GitHub API
│   │
│   ├── github/
│   │   ├── __init__.py
│   │   ├── app.py               # GitHub App JWT auth + installation tokens
│   │   ├── api.py               # GitHub API client
│   │   └── setup.py             # Interactive App creation helper
│   │
│   ├── config/
│   │   ├── __init__.py
│   │   ├── settings.py          # Pydantic settings
│   │   └── paths.py             # Standard paths (~/.review-bot/*)
│   │
│   └── utils/
│       ├── __init__.py
│       ├── git.py               # Git operations (clone, diff)
│       └── logging.py           # Structured logging
│
└── tests/
    ├── conftest.py
    ├── cli/
    ├── server/
    ├── persona/
    ├── review/
    └── github/
```

## Dependencies

```
# Core
fastapi          # Webhook server
uvicorn          # ASGI server
click            # CLI framework
pydantic         # Data models + settings
pydantic-settings # Env var config
pyyaml           # Persona profile I/O
httpx            # Async HTTP client (GitHub API)
sqlalchemy       # Database (job history, analytics)
aiosqlite        # Async SQLite driver
PyJWT            # GitHub App JWT authentication
cryptography     # GitHub App private key handling

# LLM
claude-code-sdk  # Claude Code SDK (login-based auth)

# Dev
pytest
pytest-asyncio
ruff
```

## Error Handling

- **Webhook signature validation fails:** Reject with 401, log the attempt
- **Persona not found for bot username:** Post a comment on the PR: "No persona configured for this bot. Run `review-bot persona create`"
- **Claude Code SDK fails:** Retry once. If still fails, post comment: "Review failed — LLM unavailable. Will retry automatically."
- **GitHub API rate limit:** Queue the review, retry with exponential backoff
- **Repo clone fails:** Post comment with error details, skip review
- **Invalid PR data:** Log warning, skip gracefully

## Security

- All webhooks validated via HMAC-SHA256 signature
- GitHub App private key stored in `~/.review-bot/` with 600 permissions
- No API keys stored — Claude auth is via CLI login session
- No secrets in persona YAML files
- Repo clones cached locally, never exposed

## Future Extensions (not in v1)

- Web dashboard for persona management and review analytics
- Persona approval workflow (optional — reviewer approves their bot)
- Review quality scoring (did the human reviewer agree with the bot?)
- Slack/Teams notifications when bot reviews are posted
- Auto-update personas on a schedule (cron re-mining)
- Redis + worker pool for horizontal scaling
- Fine-tuning on review patterns for even more accuracy

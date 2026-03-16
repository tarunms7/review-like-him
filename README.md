<div align="center">

<h1>review-like-him</h1>

**AI reviewer bots that sound exactly like your best engineers**

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![GitHub App](https://img.shields.io/badge/GitHub-App-purple.svg)](https://docs.github.com/en/apps)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/mtwn105/review-like-him/pulls)
[![GitHub Stars](https://img.shields.io/github/stars/mtwn105/review-like-him?style=social)](https://github.com/mtwn105/review-like-him)

</div>

Assign `deepam-bot` to your PR — get a review that sounds exactly like Deepam. The nitpicks, the architecture concerns, the tone. All learned from their real GitHub review history.

```
You  →  Assign deepam-bot to PR #42
Bot  →  Posts review in Deepam's exact style:

  🐛 Bug        "This nil check is missing the same edge case you hit in #38.
                  The error path returns early but doesn't close the connection."

  🔒 Security   "You're interpolating user input into this query. Parameterize it
                  or this WILL end up on HackerOne."

  ⚡ Performance "This N+1 inside the loop will crush you at scale. Batch the
                  lookup like we did in the catalog service."

  💅 Style      "You know how I feel about single-letter variables."

  🧪 Testing    "No tests for the sad path? Come on."

  📐 Architecture "This controller is doing too much. Extract the validation
                    into a service object."
```

---

## Table of Contents

- [Why This Exists](#why-this-exists)
- [Features](#features)
- [How It Works](#how-it-works)
- [Quick Start](#quick-start)
- [Detailed Setup Guide](#detailed-setup-guide)
- [Usage Guide](#usage-guide)
- [Configuration Reference](#configuration-reference)
- [Architecture Overview](#architecture-overview)
- [Dashboard](#dashboard)
- [Troubleshooting](#troubleshooting)
- [Tech Stack](#tech-stack)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

---

## Why This Exists

Your best code reviewer just left the team. Or maybe they're on vacation, or spread too thin across 15 repos. Either way — their institutional knowledge, their instinct for catching bugs, their way of mentoring through reviews — it's gone.

**review-like-him** mines a person's real GitHub review history, builds a persona profile capturing their tone, priorities, and pet peeves, and then uses Claude to review every PR exactly the way they would. Your team's review culture doesn't have to be a single point of failure anymore.

---

## Features

### 🎭 Persona Mining & Profiling

Mine any GitHub user's review history to build a persona that captures their unique voice. The miner fetches all accessible repos, paginates through comments, and feeds them to Claude for pattern extraction — tone, priorities, pet peeves, severity patterns, and approval criteria.

**Module:** `persona/miner.py`, `persona/analyzer.py`, `persona/profile.py`, `persona/store.py`

### ⏱️ Temporal Weighting

Not all reviews are equal. Recent reviews reflect a person's *current* opinions, not their style from three years ago. Comments from the last 3 months get **3x weight**, 3–12 months get **1.5x**, and older reviews get **0.5x** — so the persona evolves with the person.

**Module:** `persona/temporal.py`

### 🧹 Smart Comment Deduplication

Thread-aware dedup prevents the same "fixed" or "lgtm" reply from inflating the persona. Original comments get **1.0x** weight, substantive replies (100+ chars) get **0.7x**, generic replies get **0.3x**, and self-replies get **0.2x**. Trivial one-word responses are filtered entirely.

**Module:** `persona/dedup.py`

### 📦 Chunked Review Pipeline

Large PRs don't break the system. Diffs are split into reviewable chunks of **70KB / 50 files** max, with individual files capped at 30KB. Generated files (lockfiles, minified assets, vendored code) are auto-skipped. Each chunk gets its own review pass, and results are merged.

**Module:** `review/chunker.py`, `review/merger.py`

### 🎯 Severity-Based Filtering

Every finding gets a severity score (0–4) based on category and confidence. Security issues score 4, bugs score 3, architecture and performance score 2, testing and style score 1. High-confidence findings get a +1 boost, low-confidence get -1. Critical security keywords (SQL injection, RCE, path traversal) bypass all filtering.

**Module:** `review/severity.py`

### 📂 File-Type Aware Reviews

Files are classified into **9 categories** — migration, business logic, test, config, documentation, API definition, build, generated, and infrastructure — each with tailored review instructions and severity modifiers. Migration files get extra scrutiny; docs get lighter treatment.

**Module:** `review/file_strategy.py`

### 🔄 Feedback Learning Loop

After posting reviews, the bot tracks reactions (👍👎) and replies on its comments. A background poller checks for feedback on a configurable interval (default: every 6 hours), aggregates it, and stores summaries for future persona refinement.

**Module:** `review/feedback.py`, `review/feedback_poller.py`

### 👥 Multi-Persona Comparison

Run a PR through multiple personas side-by-side without posting. See where reviewers agree (strong signal) and where they differ (discussion-worthy). Runs up to 3 personas concurrently with configurable per-persona timeouts.

**Module:** `review/comparator.py`, `review/comparison_formatter.py`, `cli/compare_cmd.py`

### 🔔 Notifications (Slack & Discord)

Get notified when reviews complete. Slack notifications use Block Kit formatting with rich review summaries. Discord notifications use webhook embeds. Both support channel routing and can be toggled globally.

**Module:** `notifications/slack.py`, `notifications/discord.py`, `notifications/base.py`

### 📊 Per-Repo Configuration

Drop a `.review-like-him.yml` in any repo to customize review behavior — set default personas, severity thresholds, skip patterns for generated files, custom instructions, max comment limits, and per-persona overrides. No global config changes needed.

**Module:** `config/repo_config.py`

### 🏥 Health Probes & Monitoring

Kubernetes-ready health endpoints (`/health/live`, `/health/ready`, `/health/startup`) with uptime tracking, database connectivity checks, and queue depth monitoring. The dashboard at `/dashboard/` shows review counts, active personas, and daily trends.

**Module:** `server/health.py`, `server/status.py`, `dashboard/router.py`, `dashboard/queries.py`

---

## How It Works

### Review Pipeline

```
┌──────────┐     ┌───────────┐     ┌──────────────┐     ┌────────────┐     ┌───────────┐
│ GitHub PR │────▶│  Webhook  │────▶│ Load Persona │────▶│   Claude   │────▶│  Post as  │
│  Created  │     │  Server   │     │   Profile    │     │   Review   │     │    Bot    │
└──────────┘     └───────────┘     └──────────────┘     └────────────┘     └───────────┘
                      │                    │                    │                  │
                 Validate HMAC      Load tone, priorities   Build prompt     Summary +
                 Route event        pet peeves, severity    with persona     inline comments
                 Queue job          + repo context          + diff chunks    as PR review
```

### Persona Mining Flow

```
┌────────────┐     ┌──────────────┐     ┌────────────┐     ┌──────────┐     ┌───────────┐
│ GitHub API │────▶│  Paginated   │────▶│  Temporal   │────▶│  Dedup   │────▶│  Claude   │
│  Reviews   │     │    Fetch     │     │  Weighting  │     │ & Filter │     │ Analysis  │
└────────────┘     └──────────────┘     └────────────┘     └──────────┘     └───────────┘
                        │                     │                  │                 │
                   All accessible       3x / 1.5x / 0.5x   Thread-aware     Extract tone,
                   repos, rate-         based on recency    weight by type   priorities,
                   limited              (90d / 365d)        (1.0 / 0.7 /    pet peeves
                                                            0.3 / 0.2)      → YAML profile
```

### Step by Step

1. A developer opens a PR and assigns a persona bot as reviewer
2. The webhook server validates the HMAC signature, routes the event, and queues the job
3. The persona profile is loaded and the repo scanner detects conventions (tests, CI, linting, frameworks)
4. Large diffs are chunked (70KB / 50 files per chunk), files are classified by type, and generated files are skipped
5. Claude reviews each chunk through the persona's lens — their priorities, tone, and pet peeves
6. Chunk results are merged, severity-filtered, and the bot posts a categorized review with inline comments in the persona's voice
7. The feedback poller later tracks reactions and replies for persona refinement

---

## Quick Start

### 1. Clone and set up

```bash
git clone https://github.com/mtwn105/review-like-him.git
cd review-like-him
./setup.sh
```

### 2. Create a persona

```bash
source .venv/bin/activate
review-bot persona create deepam --github-user deepam-kapur
```

### 3. Start reviewing

```bash
# Start the webhook server
review-bot server start

# Or run a one-off review from the CLI
review-bot review https://github.com/org/repo/pull/42 --as deepam
```

Assign `deepam-bot` as a reviewer on any PR — done.

---

<details>
<summary>📖 Detailed Setup Guide</summary>

## Detailed Setup Guide

### Installing dependencies

The recommended way is the setup script, which handles everything automatically:

```bash
./setup.sh
```

What it does:

1. Checks for Python 3.11+, tells you how to install it if missing
2. Installs [uv](https://docs.astral.sh/uv/) (falls back to pip if uv install fails)
3. Creates a `.venv` virtual environment and installs the package in editable mode
4. Checks for Claude CLI and provides install instructions if missing
5. Runs `review-bot init` to configure your GitHub App

To skip the interactive init wizard (useful in CI or scripted setups):

```bash
./setup.sh --no-init
```

**Manual installation** (if you prefer):

```bash
# Using uv (recommended)
uv venv .venv --python python3.12
source .venv/bin/activate
uv pip install -e ".[dev]"

# Or using pip
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Running `review-bot init`

```bash
review-bot init
```

The interactive wizard will:

- **Create a GitHub App** — opens your browser with pre-filled settings, or connects an existing app
- **Set your webhook URL** — auto-detects [ngrok](https://ngrok.com/) for local development, or asks for your production URL
- **Verify Claude CLI** — confirms `claude` is installed and authenticated
- **Save configuration** — writes to `~/.review-bot/config.yaml`

Expected output:

```
✓ GitHub App created (ID: 123456)
✓ Webhook URL configured: https://abc123.ngrok.app/webhook
✓ Claude CLI verified
✓ Config saved to ~/.review-bot/config.yaml
✓ Setup complete.
```

### Creating a GitHub App

If you prefer to create the GitHub App manually:

1. Go to **Settings → Developer settings → [GitHub Apps → New GitHub App](https://github.com/settings/apps/new)**
2. Fill in the app details:
   - **App name**: Your choice (e.g., "ReviewLikeHim")
   - **Homepage URL**: Your server URL or repo URL
   - **Webhook URL**: Your server's public URL + `/webhook` (e.g., `https://abc123.ngrok.app/webhook`)
   - **Webhook secret**: Generate a strong secret and save it
3. Set permissions:

   | Permission | Access | Purpose |
   |---|---|---|
   | Pull requests | **Read & Write** | Post reviews and comments |
   | Issues | **Read & Write** | Post comments on PRs |
   | Contents | **Read** | Read repo files and diffs |
   | Metadata | **Read** | Read repository info |

4. Subscribe to events:
   - `pull_request` — triggers on reviewer assignment and labels
   - `pull_request_review` — review activity tracking
   - `pull_request_review_comment` — comment tracking

5. Click **Create GitHub App**
6. Note the **App ID** from the app settings page
7. Generate a **private key** — download the `.pem` file and save it:

   ```bash
   mv ~/Downloads/your-app.private-key.pem ~/.review-bot/private-key.pem
   chmod 600 ~/.review-bot/private-key.pem
   ```

8. Install the app on the repositories you want to use it with

### Setting up a webhook URL

**For local development** — use [ngrok](https://ngrok.com/):

```bash
ngrok http 8000
# Copy the forwarding URL (e.g., https://abc123.ngrok.app)
# Your webhook URL is: https://abc123.ngrok.app/webhook
```

**For production** — point your domain at the server:

- Deploy behind a reverse proxy (nginx, Caddy)
- Use a cloud provider with a static IP or domain
- Webhook URL: `https://your-domain.com/webhook`

### Environment variables

All settings use the `REVIEW_BOT_` prefix. Environment variables take precedence over `config.yaml`.

| Variable | Required | Description | Default |
|---|---|---|---|
| `REVIEW_BOT_GITHUB_APP_ID` | **Yes** | Your GitHub App's ID | — |
| `REVIEW_BOT_PRIVATE_KEY_PATH` | **Yes** | Path to the App's private key `.pem` file | `~/.review-bot/private-key.pem` |
| `REVIEW_BOT_WEBHOOK_SECRET` | **Yes** | Webhook HMAC secret (set during App creation) | — |
| `REVIEW_BOT_WEBHOOK_URL` | No | Public URL for webhooks | — |
| `REVIEW_BOT_DB_URL` | No | SQLite database URL | `~/.review-bot/review-bot.db` |
| `REVIEW_BOT_HOST` | No | Server bind address | `0.0.0.0` |
| `REVIEW_BOT_PORT` | No | Server bind port | `8000` |
| `REVIEW_BOT_MIN_SEVERITY` | No | Min severity threshold (0=all, 4=critical only) | `0` |
| `REVIEW_BOT_FEEDBACK_POLL_INTERVAL_HOURS` | No | Hours between feedback polling | `6` |
| `REVIEW_BOT_MAX_PERSONAS_PER_PR` | No | Max personas per PR (1-20) | `5` |
| `REVIEW_BOT_SLACK_WEBHOOK_URL` | No | Slack webhook URL | — |
| `REVIEW_BOT_SLACK_BOT_TOKEN` | No | Slack bot token (xoxb-...) | — |
| `REVIEW_BOT_SLACK_CHANNEL` | No | Slack channel (e.g., #reviews) | — |
| `REVIEW_BOT_DISCORD_WEBHOOK_URL` | No | Discord webhook URL | — |
| `REVIEW_BOT_NOTIFICATIONS_ENABLED` | No | Enable notifications | `false` |
| `REVIEW_BOT_SHUTDOWN_DRAIN_TIMEOUT` | No | Seconds for graceful shutdown drain | `30` |

You can set these in your shell profile, a `.env` file, or pass them directly:

```bash
export REVIEW_BOT_GITHUB_APP_ID=123456
export REVIEW_BOT_WEBHOOK_SECRET=your-secret-here
```

</details>

---

<details>
<summary>📘 Usage Guide</summary>

## Usage Guide

### Creating personas

Mine a GitHub user's review history to create a persona:

```bash
review-bot persona create deepam --github-user deepam-kapur
```

This will:

1. Search all accessible repos for the user's PR reviews
2. Fetch inline comments and review verdicts (with pagination and rate limiting)
3. Apply temporal weighting — recent reviews (last 3 months) carry 3x weight
4. Deduplicate threads — original comments 1.0x, substantive replies 0.7x, generic replies 0.3x, self-replies 0.2x
5. Use Claude to analyze patterns and extract the persona profile
6. Save to `~/.review-bot/personas/deepam.yaml`

You'll see a preview of what the bot learned:

```
Mining review history for deepam-kapur...
  Found 847 comments across 12 repos
  Analyzing patterns with Claude...

Here's what I learned about Deepam:
  Tone: Direct but supportive, uses humor
  Top priorities: error handling (critical), test coverage (strict)
  Pet peeves: magic numbers, catch-all exceptions, commented-out code

✓ Persona saved to ~/.review-bot/personas/deepam.yaml
```

### Managing personas

```bash
# List all personas
review-bot persona list

# Show full persona details
review-bot persona show deepam

# Re-mine with latest reviews (incremental update)
review-bot persona update deepam

# Edit persona YAML manually (opens $EDITOR)
review-bot persona edit deepam
```

Manual edits to persona YAML are preserved when you run `persona update` — only mined data is refreshed.

### Triggering reviews

There are three ways to trigger a review on a PR:

**1. Assign as reviewer** — Add `deepam-bot` as a reviewer on the PR in GitHub's UI.

**2. Comment command** — Post a comment on the PR:

```
/review-as deepam
/review-as deepam,sarah       # multiple personas
/review-as deepam sarah        # also works
```

**3. Label** — Add a label matching `review:<persona>`:

```
review:deepam
```

Each persona reviews independently — overlap between reviewers is signal, not noise.

### Manual CLI review

Run a one-off review from the command line without the webhook server:

```bash
review-bot review https://github.com/org/repo/pull/42 --as deepam
```

### Comparing personas

Run a PR through multiple personas side-by-side without posting to GitHub:

```bash
review-bot compare <PR_URL> --personas <name1,name2> [--timeout 120] [--json-output]
```

Examples:

```bash
# Compare how deepam and sarah would review the same PR
review-bot compare https://github.com/org/repo/pull/42 --personas deepam,sarah

# Get results as JSON
review-bot compare https://github.com/org/repo/pull/42 -p deepam,sarah,alex --json-output

# Custom timeout per persona (seconds)
review-bot compare https://github.com/org/repo/pull/42 -p deepam,sarah --timeout 180
```

### Per-repo configuration

Drop a `.review-like-him.yml` in the root of any repo to customize review behavior:

```yaml
version: 1
persona: deepam
min_severity: medium
skip_patterns:
  - "*.generated.*"
  - "vendor/**"
custom_instructions: "Focus on error handling in API routes"
max_comments: 30
persona_overrides:
  sarah:
    min_severity: low
    custom_instructions: "Also check test coverage"
```

### Starting the server

```bash
# Start in foreground
review-bot server start

# Custom host and port
review-bot server start --host 127.0.0.1 --port 9000

# Start as background daemon
review-bot server start --daemon

# Check server status
review-bot server status

# Tail recent review logs
review-bot server logs -n 50
```

</details>

---

<details>
<summary>⚙️ Configuration Reference</summary>

## Configuration Reference

### Config file

Main configuration lives at `~/.review-bot/config.yaml`:

```yaml
github:
  app_id: 123456
  private_key_path: ~/.review-bot/private-key.pem
  webhook_secret: your-secret-here
  webhook_url: https://abc123.ngrok.app/webhook

server:
  host: 0.0.0.0
  port: 8000

database:
  url: sqlite:///~/.review-bot/review-bot.db
```

Environment variables (with `REVIEW_BOT_` prefix) override config file values.

### Directory structure

```
~/.review-bot/
├── config.yaml          # Main configuration
├── private-key.pem      # GitHub App private key (chmod 600)
├── review-bot.db        # SQLite database (review history, job queue)
├── personas/            # Persona YAML profiles
│   ├── deepam.yaml
│   └── sarah.yaml
└── repos/               # Cached repo data
```

### Persona YAML format

```yaml
name: deepam
github_user: deepam-kapur
mined_from: 847 comments across 12 repos
last_updated: 2026-03-13

priorities:
  - category: error_handling
    severity: critical           # critical | strict | moderate | opinionated
    description: "Always flags missing error paths"
  - category: test_coverage
    severity: strict
    description: "Won't approve without tests for new logic"

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
  approves_when: "logic is sound, tests exist, errors are handled"

overrides:                       # manual additions, preserved on re-mine
  - "Extra strict about database migrations"
```

### Per-repo config (`.review-like-him.yml`)

Place this file in the root of any repository to customize review behavior:

```yaml
version: 1                       # Config version (required, must be 1)
persona: deepam                  # Default persona for this repo
min_severity: medium             # low | medium | high | critical
skip_patterns:                   # Glob patterns for files to skip
  - "*.generated.*"
  - "vendor/**"
  - "*.min.js"
custom_instructions: ""          # Extra instructions appended to the review prompt
max_comments: 50                 # Max inline comments to post (1-100, default 50)
persona_overrides:               # Per-persona overrides
  sarah:
    min_severity: low
    custom_instructions: "Also check test coverage"
    skip_patterns:
      - "docs/**"
    max_comments: 30
```

### Environment variables (complete reference)

All settings use the `REVIEW_BOT_` prefix. Environment variables take precedence over `config.yaml`.

| Variable | Required | Description | Default |
|---|---|---|---|
| `REVIEW_BOT_GITHUB_APP_ID` | **Yes** | Your GitHub App's ID | — |
| `REVIEW_BOT_PRIVATE_KEY_PATH` | **Yes** | Path to the App's private key `.pem` file | `~/.review-bot/private-key.pem` |
| `REVIEW_BOT_WEBHOOK_SECRET` | **Yes** | Webhook HMAC secret (set during App creation) | — |
| `REVIEW_BOT_WEBHOOK_URL` | No | Public URL for webhooks | — |
| `REVIEW_BOT_DB_URL` | No | SQLite database URL | `~/.review-bot/review-bot.db` |
| `REVIEW_BOT_HOST` | No | Server bind address | `0.0.0.0` |
| `REVIEW_BOT_PORT` | No | Server bind port | `8000` |
| `REVIEW_BOT_MIN_SEVERITY` | No | Min severity threshold (0=all, 4=critical only) | `0` |
| `REVIEW_BOT_FEEDBACK_POLL_INTERVAL_HOURS` | No | Hours between feedback polling | `6` |
| `REVIEW_BOT_MAX_PERSONAS_PER_PR` | No | Max personas per PR (1-20) | `5` |
| `REVIEW_BOT_SLACK_WEBHOOK_URL` | No | Slack webhook URL | — |
| `REVIEW_BOT_SLACK_BOT_TOKEN` | No | Slack bot token (xoxb-...) | — |
| `REVIEW_BOT_SLACK_CHANNEL` | No | Slack channel (e.g., #reviews) | — |
| `REVIEW_BOT_DISCORD_WEBHOOK_URL` | No | Discord webhook URL | — |
| `REVIEW_BOT_NOTIFICATIONS_ENABLED` | No | Enable notifications | `false` |
| `REVIEW_BOT_SHUTDOWN_DRAIN_TIMEOUT` | No | Seconds for graceful shutdown drain | `30` |

</details>

---

## Architecture Overview

```
review_bot/
├── cli/                          # Click CLI commands
│   ├── main.py                   # Command group entry point
│   ├── init_cmd.py               # Interactive setup wizard
│   ├── persona_cmd.py            # Persona CRUD commands
│   ├── review_cmd.py             # Manual review trigger
│   ├── compare_cmd.py            # Multi-persona comparison
│   ├── db_cmd.py                 # Database management commands
│   ├── server_cmd.py             # Server start/status/logs
│   ├── status_cmd.py             # Status display
│   └── utils.py                  # Shared CLI utilities
├── config/                       # Configuration management
│   ├── paths.py                  # Default file/directory paths (~/.review-bot/*)
│   ├── settings.py               # Pydantic BaseSettings with env var binding
│   └── repo_config.py            # Per-repo .review-like-him.yml config
├── db/                           # Database layer
│   ├── __init__.py               # DB initialization
│   └── migration.py              # Schema migration management
├── github/                       # GitHub integration
│   ├── api.py                    # Async GitHub API client (httpx)
│   ├── app.py                    # GitHub App JWT auth & installation token caching
│   ├── rate_limits.py            # Rate limit tracking and retry logic
│   └── setup.py                  # Interactive App creation helper
├── persona/                      # Persona engine
│   ├── analyzer.py               # Claude-powered review pattern extraction
│   ├── dedup.py                  # Thread-aware comment deduplication & weighting
│   ├── miner.py                  # GitHub review history mining (paginated, rate-limited)
│   ├── profile.py                # Persona data models (Pydantic)
│   ├── store.py                  # YAML persistence for persona profiles
│   └── temporal.py               # Time-based review weighting (recent = higher weight)
├── review/                       # Review pipeline
│   ├── chunker.py                # Multi-pass diff chunker (70KB / 50 files per chunk)
│   ├── comparator.py             # Multi-persona side-by-side comparison
│   ├── comparison_formatter.py   # Format comparison results for display
│   ├── feedback.py               # Reaction/reply tracking and storage
│   ├── feedback_poller.py        # Background feedback polling loop
│   ├── file_strategy.py          # File-type classification (9 types) & review strategy
│   ├── formatter.py              # LLM output → structured categorized review
│   ├── github_poster.py          # Post reviews + inline comments via GitHub API
│   ├── merger.py                 # Merge chunked review results
│   ├── orchestrator.py           # End-to-end review coordination
│   ├── prompt_builder.py         # Persona + repo context + diff → Claude prompt
│   ├── repo_scanner.py           # Auto-detect repo conventions (tests, CI, linting)
│   ├── reviewer.py               # Claude Code SDK integration
│   └── severity.py               # Severity scoring and filtering (0–4 scale)
├── server/                       # Webhook server
│   ├── app.py                    # FastAPI application factory
│   ├── health.py                 # Health check endpoints (liveness/readiness/startup)
│   ├── queue.py                  # Async job queue (asyncio, upgradeable to Redis)
│   ├── status.py                 # Server status reporting
│   └── webhooks.py               # Event routing & HMAC signature validation
├── dashboard/                    # Web dashboard
│   ├── __init__.py               # Dashboard initialization
│   ├── router.py                 # FastAPI router with Jinja2 template rendering
│   ├── queries.py                # Database queries for dashboard data
│   ├── static/                   # CSS assets
│   │   └── style.css
│   └── templates/                # Jinja2 HTML templates
│       ├── base.html
│       ├── overview.html
│       ├── activity.html
│       ├── personas.html
│       ├── queue.html
│       └── config.html
├── notifications/                # Notification dispatch
│   ├── __init__.py               # Notification initialization
│   ├── base.py                   # Base notifier interface
│   ├── slack.py                  # Slack notifications (Block Kit)
│   └── discord.py                # Discord notifications (webhook embeds)
└── utils/                        # Shared utilities
    ├── git.py                    # Git operations (clone, diff)
    └── logging.py                # Structured logging setup
```

### Key design decisions

- **Single GitHub App** — one app, multiple personas. No separate accounts per reviewer.
- **Claude Code SDK** — uses CLI auth, no API keys to manage.
- **Async throughout** — httpx, aiosqlite, FastAPI for non-blocking I/O.
- **Chunked review pipeline** — large PRs are split into manageable chunks, reviewed independently, and merged back together.
- **Feedback learning loop** — bot tracks reactions and replies on its own comments for continuous persona refinement.
- **Multi-persona comparison** — compare how different reviewers would approach the same PR without posting.
- **Health probes** — Kubernetes-ready liveness, readiness, and startup probes for production deployments.
- **Notification dispatch** — pluggable notification system with Slack and Discord adapters.
- **Monolith-ready for scale** — module boundaries allow swapping SQLite → PostgreSQL and asyncio queue → Redis with config changes.
- **Repo-aware reviews** — the bot scans each repo's conventions before reviewing, so it adapts to context (won't demand tests in repos without test infrastructure).

---

## Dashboard

The built-in web dashboard provides visibility into the review bot's activity and health.

### Accessing the dashboard

Navigate to `http://your-server:8000/dashboard/` when the server is running.

### Pages

| Page | Path | Description |
|---|---|---|
| **Overview** | `/dashboard/` | Review counts (24h / 7d / 30d), active personas, queue depth |
| **Activity** | `/dashboard/activity` | Timeline of recent reviews with filters |
| **Personas** | `/dashboard/personas` | Persona stats and usage metrics |
| **Queue** | `/dashboard/queue` | Current job queue with status |
| **Config** | `/dashboard/config` | Active configuration display |

The dashboard uses Jinja2 templates with server-side rendering and includes daily trend charts, persona breakdown, and activity timeline with date-range filters.

---

<details>
<summary>🔧 Troubleshooting</summary>

## Troubleshooting

### Webhook not receiving events

| Symptom | Solution |
|---|---|
| No events arriving at server | Verify your webhook URL is publicly accessible. Check with `curl -X POST https://your-url/webhook` |
| ngrok URL stopped working | ngrok free URLs rotate on restart. Update the webhook URL in your GitHub App settings and `config.yaml` |
| Events arriving but 401 response | Webhook secret mismatch. Ensure `REVIEW_BOT_WEBHOOK_SECRET` matches what's set in the GitHub App |
| Events arriving but no review posted | Check `review-bot server logs` for errors. The persona may not exist for the assigned bot name |

### Persona mining fails

| Symptom | Solution |
|---|---|
| "Rate limit exceeded" | The miner respects rate limits automatically. If it fails, wait and retry. Check `X-RateLimit-Reset` header timing |
| "No reviews found" | The GitHub user may not have public review activity, or your GitHub App may not have access to their repos. Verify app installation |
| Mining is very slow | Large review histories take time. The miner paginates and applies rate limiting. Use `--verbose` for progress details |
| Claude analysis fails | Ensure Claude CLI is authenticated: run `claude` in your terminal. If session expired, run `claude login` |

### Review not posting

| Symptom | Solution |
|---|---|
| "Persona not found" | The persona name in the assignment must match an existing persona. Check `review-bot persona list` |
| "Permission denied" posting review | The GitHub App needs **Read & Write** access to Pull Requests. Check app permissions in GitHub settings |
| Review posts as comment instead of review | This is a fallback — the PR review API may have failed. Check logs for the original error |
| "Claude session expired" | Run `claude login` to re-authenticate. The server will retry automatically on the next event |

### General issues

| Symptom | Solution |
|---|---|
| `review-bot: command not found` | Activate the virtual environment: `source .venv/bin/activate` |
| Import errors on startup | Re-run `uv pip install -e ".[dev]"` or `pip install -e ".[dev]"` |
| Database locked errors | Another instance may be running. Check `review-bot server status` and stop duplicates |
| Config not loading | Ensure `~/.review-bot/config.yaml` exists and is valid YAML. Environment variables override config values |

</details>

---

## Tech Stack

| Component | Technology |
|---|---|
| **CLI** | [Click](https://click.palletsprojects.com/) + [Rich](https://rich.readthedocs.io/) |
| **Server** | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) |
| **LLM** | [Claude Code SDK](https://docs.anthropic.com/) |
| **Database** | [SQLite](https://www.sqlite.org/) + [SQLAlchemy](https://www.sqlalchemy.org/) + [aiosqlite](https://github.com/omnilib/aiosqlite) |
| **Config** | [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) |
| **HTTP** | [httpx](https://www.python-httpx.org/) |
| **Auth** | [PyJWT](https://pyjwt.readthedocs.io/) + [cryptography](https://cryptography.io/) |
| **Dashboard** | [Jinja2](https://jinja.palletsprojects.com/) templates |
| **Notifications** | Slack ([Block Kit](https://api.slack.com/block-kit)) + Discord (webhooks) |
| **Task Queue** | asyncio (upgradeable to Redis) |
| **Testing** | [pytest](https://docs.pytest.org/) + [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) + [respx](https://github.com/lundberg/respx) + [pytest-cov](https://github.com/pytest-dev/pytest-cov) |
| **Linting** | [Ruff](https://docs.astral.sh/ruff/) |

---

## Development

### Dev setup

```bash
git clone https://github.com/mtwn105/review-like-him.git
cd review-like-him
uv pip install -e ".[dev]"
```

### Running tests

```bash
pytest
```

The test suite includes 27 test files covering all modules. Tests use pytest-asyncio in auto mode for async tests and respx for HTTP mocking. See `pyproject.toml` for the full dev dependency list.

### Linting

```bash
ruff check .
ruff format .
```

Ruff config: line length 100, target Python 3.11, rules: E, F, I, N, W, UP.

---

## Contributing

Contributions are welcome! Here's the workflow:

1. Fork the repo
2. Create a feature branch (`git checkout -b feat/my-feature`)
3. Make your changes
4. Ensure `ruff check .` and `ruff format --check .` pass
5. Ensure `pytest` passes
6. Open a PR

---

## License

[MIT](LICENSE)

---

<div align="center">

Built with ❤️ and Claude

If this project helps your team, consider giving it a ⭐

</div>

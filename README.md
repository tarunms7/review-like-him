# 🤖 review-like-him

**AI-powered reviewer bots that mimic real people's code review style.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![GitHub App](https://img.shields.io/badge/GitHub-App-purple.svg)](https://docs.github.com/en/apps)

---

## What is this?

**review-like-him** creates AI reviewer bots that learn and replicate how specific people review code. It mines a developer's past GitHub reviews, builds a persona profile capturing their tone, priorities, and pet peeves, then uses Claude to deliver reviews in their style.

Assign `deepam-bot` to your PR — get a review that sounds exactly like Deepam. Complete with the nitpicks, the architecture concerns, and the communication style you'd expect.

```
You  →  Assign deepam-bot to PR #42
Bot  →  Posts review in Deepam's exact style
         🐛 "This nil check is missing the same edge case you hit in #38..."
         💅 "You know how I feel about single-letter variables."
```

## How It Works

```
┌──────────┐     ┌───────────┐     ┌──────────────┐     ┌────────────┐     ┌───────────┐
│ GitHub PR │────▶│  Webhook  │────▶│ Load Persona │────▶│   Claude   │────▶│  Post as  │
│  Created  │     │  Server   │     │   Profile    │     │   Review   │     │    Bot    │
└──────────┘     └───────────┘     └──────────────┘     └────────────┘     └───────────┘
                      │                    │                    │                  │
                 Validate HMAC      Load tone, priorities   Build prompt     Summary +
                 Route event        pet peeves, severity    with persona     inline comments
                 Queue job          + repo context          + diff           as PR review
```

## Quick Start

### One-command setup

Clone the repo and run the setup script:

```bash
git clone https://github.com/your-org/review-like-him.git
cd review-like-him
./setup.sh
```

The setup script automatically:
1. Checks for Python 3.11+
2. Installs the `uv` package manager (or falls back to pip)
3. Creates a virtual environment and installs dependencies
4. Checks for Claude CLI
5. Runs `review-bot init` to configure your GitHub App

### Three commands to your first review

```bash
review-bot persona create deepam --github-user deepam-kapur
review-bot server start
# Assign deepam-bot as reviewer on any PR — done!
```

## Manual Installation

### Prerequisites

- **Python 3.11+**
- **Claude CLI** — [install instructions](https://docs.anthropic.com/en/docs/claude-cli)
- **A GitHub account** with permission to create GitHub Apps

### Install

```bash
# Using uv (recommended)
uv pip install -e ".[dev]"

# Or using pip
pip install -e ".[dev]"
```

### Initialize

```bash
review-bot init
```

The interactive wizard will guide you through:
- Creating a GitHub App (or connecting an existing one)
- Setting your webhook URL (auto-detects ngrok)
- Saving config to `~/.review-bot/config.yaml`

## Configuration

### Environment Variables

All settings use the `REVIEW_BOT_` prefix:

| Variable | Description | Default |
|----------|-------------|---------|
| `REVIEW_BOT_GITHUB_APP_ID` | Your GitHub App ID | — |
| `REVIEW_BOT_PRIVATE_KEY_PATH` | Path to App private key PEM | `~/.review-bot/private-key.pem` |
| `REVIEW_BOT_WEBHOOK_SECRET` | Webhook HMAC secret | — |
| `REVIEW_BOT_WEBHOOK_URL` | Public URL for webhooks | — |
| `REVIEW_BOT_DB_URL` | SQLite database URL | `~/.review-bot/review-bot.db` |
| `REVIEW_BOT_HOST` | Server bind host | `0.0.0.0` |
| `REVIEW_BOT_PORT` | Server bind port | `8000` |

### Config File

Config is stored at `~/.review-bot/config.yaml`. Environment variables take precedence over the config file.

### Directory Structure

```
~/.review-bot/
├── config.yaml          # Main configuration
├── private-key.pem      # GitHub App private key
├── review-bot.db        # SQLite database
├── personas/            # Persona YAML profiles
│   ├── deepam.yaml
│   └── sarah.yaml
└── repos/               # Cached repo data
```

## Usage Guide

### Creating Personas

Mine a GitHub user's review history to create a persona:

```bash
review-bot persona create deepam --github-user deepam-kapur
```

This will:
1. Search all accessible repos for the user's PR reviews
2. Fetch inline comments and review verdicts
3. Apply temporal weighting (recent reviews matter more)
4. Use Claude to analyze patterns and extract the persona profile
5. Save to `~/.review-bot/personas/deepam.yaml`

### Managing Personas

```bash
# List all personas
review-bot persona list

# Show full persona details
review-bot persona show deepam

# Re-mine and update with latest reviews
review-bot persona update deepam

# Edit persona YAML manually (opens $EDITOR)
review-bot persona edit deepam
```

### Starting the Server

```bash
# Start in foreground
review-bot server start

# Start with custom host/port
review-bot server start --host 127.0.0.1 --port 9000

# Start as background daemon
review-bot server start --daemon

# Check server status
review-bot server status

# Tail recent review logs
review-bot server logs -n 50
```

### Triggering Reviews

There are three ways to trigger a review on a PR:

**1. Assign as reviewer** — Add `deepam-bot` as a reviewer on the PR

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

### Manual Review

Run a one-off review from the command line:

```bash
review-bot review https://github.com/org/repo/pull/42 --as deepam
```

## GitHub App Setup

### Required Permissions

| Permission | Access | Purpose |
|------------|--------|---------|
| Pull requests | **Write** | Post reviews and comments |
| Issues | **Write** | Post comments on PRs |
| Contents | **Read** | Read repo files and diffs |
| Metadata | **Read** | Read repository info |

### Webhook Events

Subscribe to these events:

- `pull_request` — triggers on reviewer assignment and labels
- `pull_request_review` — review activity tracking
- `pull_request_review_comment` — comment tracking

### Setup Steps

1. Go to [GitHub App settings](https://github.com/settings/apps/new)
2. Set the webhook URL to your server's public URL
3. Select the permissions and events listed above
4. Generate a private key and save it to `~/.review-bot/private-key.pem`
5. Note the App ID and webhook secret
6. Run `review-bot init` or set the environment variables

## Architecture

```
review_bot/
├── cli/                 # Click CLI commands
│   ├── main.py          # Command group entry point
│   ├── init_cmd.py      # Interactive setup wizard
│   ├── persona_cmd.py   # Persona CRUD commands
│   ├── review_cmd.py    # Manual review trigger
│   └── server_cmd.py    # Server start/status/logs
├── config/              # Configuration management
│   ├── paths.py         # Default file/directory paths
│   └── settings.py      # Pydantic BaseSettings with env vars
├── github/              # GitHub integration
│   ├── api.py           # Async GitHub API client (httpx)
│   ├── app.py           # GitHub App JWT auth & token caching
│   └── setup.py         # App creation helper
├── persona/             # Persona engine
│   ├── analyzer.py      # Claude-powered pattern extraction
│   ├── miner.py         # GitHub review history mining
│   ├── profile.py       # Persona data models
│   ├── store.py         # YAML persistence
│   └── temporal.py      # Time-based review weighting
├── review/              # Review pipeline
│   ├── formatter.py     # Structured output formatting
│   ├── github_poster.py # Post reviews to GitHub API
│   ├── orchestrator.py  # End-to-end review coordination
│   ├── prompt_builder.py# Persona + context → Claude prompt
│   ├── repo_scanner.py  # Detect repo conventions
│   └── reviewer.py      # Claude Code SDK integration
├── server/              # Webhook server
│   ├── app.py           # FastAPI application factory
│   ├── queue.py         # Async job queue & worker
│   └── webhooks.py      # Event routing & validation
└── utils/               # Shared utilities
    ├── git.py           # Git operations
    └── logging.py       # Structured logging
```

### Key Design Decisions

- **Single GitHub App** — One app, multiple personas. No separate accounts needed per reviewer.
- **Claude Code SDK** — Uses CLI auth, no API keys to manage.
- **Async throughout** — httpx, aiosqlite, FastAPI for non-blocking I/O.
- **Monolith-ready for scale** — Module boundaries allow swapping SQLite → PostgreSQL and asyncio queue → Redis with config changes.

## Persona System

### How Mining Works

1. **Discovery** — Searches GitHub for all repos where the user has review activity
2. **Collection** — Fetches inline review comments and PR review verdicts with pagination
3. **Rate limiting** — Monitors `X-RateLimit-Remaining`, sleeps when near the limit
4. **Conditional requests** — Uses ETags to minimize redundant API calls

### Temporal Weighting

Recent reviews influence the persona more heavily:

| Review Age | Weight | Rationale |
|------------|--------|-----------|
| **≤ 3 months** | **3.0×** | Current style and priorities |
| **3–12 months** | **1.5×** | Established patterns |
| **> 12 months** | **0.5×** | May reflect outdated preferences |

### What Gets Extracted

- **Tone** — Communication style (direct, diplomatic, sarcastic, thorough, etc.)
- **Priorities** — What they care about, with severity levels (critical, strict, moderate, opinionated)
- **Pet peeves** — Specific things that always trigger comments
- **Severity pattern** — What they block on, what they nit on, when they approve

### Manual Overrides

Edit any persona YAML to fine-tune the profile. Manual overrides are preserved when re-mining:

```bash
review-bot persona edit deepam
```

## Review Output Format

Reviews are posted as structured GitHub PR reviews with categorized findings:

### Categories

| Emoji | Category | What it covers |
|-------|----------|---------------|
| 🐛 | **Bugs** | Logic errors, race conditions, edge cases |
| 🏗️ | **Architecture** | Structural concerns, SRP violations |
| 🧪 | **Testing** | Missing coverage, weak assertions (only if repo has tests) |
| 💅 | **Style** | Naming, formatting, persona pet peeves |
| 🔒 | **Security** | Vulnerabilities, injection risks |
| ⚡ | **Performance** | Inefficiencies, N+1 queries |

### Verdicts

| Verdict | Meaning |
|---------|---------|
| `approve` | Looks good, ship it |
| `request_changes` | Blocking issues found |
| `comment` | Non-blocking feedback |

### Repo-Aware Reviews

The review pipeline scans the target repo to detect:
- Test framework and coverage tooling
- CI configuration
- Linting setup
- Language and frameworks

This ensures reviews are contextual — the bot won't demand tests if the repo has no test infrastructure.

## Development

### Dev Setup

```bash
git clone https://github.com/your-org/review-like-him.git
cd review-like-him
uv pip install -e ".[dev]"
```

### Running Tests

```bash
pytest
```

### Linting

```bash
ruff check .
ruff format .
```

Ruff is configured with:
- Line length: 100
- Target: Python 3.11
- Rules: E, F, I, N, W, UP

## Tech Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **CLI** | [Click](https://click.palletsprojects.com/) | Command-line interface |
| **Server** | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) | Webhook listener |
| **LLM** | [Claude Code SDK](https://docs.anthropic.com/) | AI-powered reviews |
| **Database** | [SQLite](https://www.sqlite.org/) + [SQLAlchemy](https://www.sqlalchemy.org/) + [aiosqlite](https://github.com/omnilib/aiosqlite) | Async persistence |
| **Config** | [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | Type-safe configuration |
| **HTTP** | [httpx](https://www.python-httpx.org/) | Async GitHub API client |
| **Auth** | [PyJWT](https://pyjwt.readthedocs.io/) + [cryptography](https://cryptography.io/) | GitHub App authentication |

## License

[MIT](LICENSE) — do whatever you want with it.

# review-like-him

**AI reviewer bots that mimic how real people review code.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![GitHub App](https://img.shields.io/badge/GitHub-App-purple.svg)](https://docs.github.com/en/apps)

Assign `deepam-bot` to your PR — get a review that sounds exactly like Deepam. The nitpicks, the architecture concerns, the tone. All learned from their real GitHub review history.

```
You  →  Assign deepam-bot to PR #42
Bot  →  Posts review in Deepam's exact style
         🐛 "This nil check is missing the same edge case you hit in #38..."
         💅 "You know how I feel about single-letter variables."
```

---

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

1. A developer opens a PR and assigns a persona bot as reviewer
2. The webhook server validates the event, loads the matching persona profile
3. The repo scanner detects conventions (tests, CI, linting, frameworks)
4. Claude reviews the diff through the persona's lens — their priorities, tone, and pet peeves
5. The bot posts a categorized review with inline comments in the persona's voice

---

## Prerequisites

Before you begin, make sure you have:

| Requirement | Version | Check |
|---|---|---|
| **Python** | 3.11 or higher | `python3 --version` |
| **Claude CLI** | Latest | `claude --version` |
| **GitHub account** | — | With permission to create GitHub Apps |
| **Node.js** (for Claude CLI) | 18+ | `node --version` |

> **Don't have Claude CLI?** Install it: `npm install -g @anthropic-ai/claude-code`

---

## Quick Start

### 1. Clone and set up

```bash
git clone https://github.com/your-org/review-like-him.git
cd review-like-him
./setup.sh
```

The setup script checks your Python version, installs dependencies in a virtual environment, and walks you through initial configuration.

### 2. Create a persona

```bash
source .venv/bin/activate
review-bot persona create deepam --github-user deepam-kapur
```

This mines the user's GitHub review history, analyzes their patterns with Claude, and saves a persona profile.

### 3. Start reviewing

```bash
# Start the webhook server
review-bot server start

# Or run a one-off review from the CLI
review-bot review https://github.com/org/repo/pull/42 --as deepam
```

Assign `deepam-bot` as a reviewer on any PR — done.

---

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

You can set these in your shell profile, a `.env` file, or pass them directly:

```bash
export REVIEW_BOT_GITHUB_APP_ID=123456
export REVIEW_BOT_WEBHOOK_SECRET=your-secret-here
```

---

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
4. Use Claude to analyze patterns and extract the persona profile
5. Save to `~/.review-bot/personas/deepam.yaml`

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

---

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

---

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

---

## Architecture Overview

```
review_bot/
├── cli/                 # Click CLI commands
│   ├── main.py          # Command group entry point
│   ├── init_cmd.py      # Interactive setup wizard
│   ├── persona_cmd.py   # Persona CRUD commands
│   ├── review_cmd.py    # Manual review trigger
│   └── server_cmd.py    # Server start/status/logs
├── config/              # Configuration management
│   ├── paths.py         # Default file/directory paths (~/.review-bot/*)
│   └── settings.py      # Pydantic BaseSettings with env var binding
├── github/              # GitHub integration
│   ├── api.py           # Async GitHub API client (httpx)
│   ├── app.py           # GitHub App JWT auth & installation token caching
│   └── setup.py         # Interactive App creation helper
├── persona/             # Persona engine
│   ├── analyzer.py      # Claude-powered review pattern extraction
│   ├── miner.py         # GitHub review history mining (paginated, rate-limited)
│   ├── profile.py       # Persona data models (Pydantic)
│   ├── store.py         # YAML persistence for persona profiles
│   └── temporal.py      # Time-based review weighting (recent = higher weight)
├── review/              # Review pipeline
│   ├── formatter.py     # LLM output → structured categorized review
│   ├── github_poster.py # Post reviews + inline comments via GitHub API
│   ├── orchestrator.py  # End-to-end review coordination
│   ├── prompt_builder.py# Persona + repo context + diff → Claude prompt
│   ├── repo_scanner.py  # Auto-detect repo conventions (tests, CI, linting)
│   └── reviewer.py      # Claude Code SDK integration
├── server/              # Webhook server
│   ├── app.py           # FastAPI application factory
│   ├── queue.py         # Async job queue (asyncio, upgradeable to Redis)
│   └── webhooks.py      # Event routing & HMAC signature validation
└── utils/               # Shared utilities
    ├── git.py           # Git operations (clone, diff)
    └── logging.py       # Structured logging setup
```

### Key design decisions

- **Single GitHub App** — one app, multiple personas. No separate accounts per reviewer.
- **Claude Code SDK** — uses CLI auth, no API keys to manage.
- **Async throughout** — httpx, aiosqlite, FastAPI for non-blocking I/O.
- **Monolith-ready for scale** — module boundaries allow swapping SQLite → PostgreSQL and asyncio queue → Redis with config changes.
- **Repo-aware reviews** — the bot scans each repo's conventions before reviewing, so it adapts to context (won't demand tests in repos without test infrastructure).

---

## Development

### Dev setup

```bash
git clone https://github.com/your-org/review-like-him.git
cd review-like-him
uv pip install -e ".[dev]"
```

### Running tests

```bash
pytest
```

### Linting

```bash
ruff check .
ruff format .
```

Ruff config: line length 100, target Python 3.11, rules: E, F, I, N, W, UP.

## Tech Stack

| Component | Technology |
|---|---|
| **CLI** | [Click](https://click.palletsprojects.com/) |
| **Server** | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) |
| **LLM** | [Claude Code SDK](https://docs.anthropic.com/) |
| **Database** | [SQLite](https://www.sqlite.org/) + [SQLAlchemy](https://www.sqlalchemy.org/) + [aiosqlite](https://github.com/omnilib/aiosqlite) |
| **Config** | [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) |
| **HTTP** | [httpx](https://www.python-httpx.org/) |
| **Auth** | [PyJWT](https://pyjwt.readthedocs.io/) + [cryptography](https://cryptography.io/) |

## License

[MIT](LICENSE)

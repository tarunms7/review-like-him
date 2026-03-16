# review-like-him

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![GitHub App](https://img.shields.io/badge/GitHub-App-purple.svg)](https://docs.github.com/en/apps)

**AI code reviews that sound like your team.**

Point it at a teammate's GitHub review history, and it learns how they review — their tone, priorities, pet peeves, everything. Then it reviews PRs the way they would, powered by Claude.

```
You  →  Assign deepam-bot to PR #42
Bot  →  Posts review in Deepam's exact style
         🐛 "This nil check is missing the same edge case you hit in #38..."
         💅 "You know how I feel about single-letter variables."
```

---

## Get Started

```bash
git clone https://github.com/your-org/review-like-him.git && cd review-like-him
./setup.sh
```

That's it. The script handles Python, dependencies, and walks you through connecting your GitHub App.

---

## What It Does

- **Mines any GitHub user's review history** to learn their patterns, tone, and pet peeves
- **Generates Claude-powered code reviews** that sound like that person
- **Auto-reviews PRs via webhook** when a persona-bot is assigned as reviewer
- **Supports multiple personas** — compare how different reviewers would see the same PR
- **Filters feedback by severity** (blocking vs. nits) based on learned patterns
- **Handles large PRs** by chunking diffs intelligently
- **Sends notifications** to Slack or Discord when reviews are posted
- **Web dashboard** for review history and persona management

---

## Quick Start

Set up your GitHub App connection:

```bash
review-bot init
```

The interactive wizard creates a GitHub App, configures your webhook URL, verifies Claude CLI, and saves everything to `~/.review-bot/config.yaml`.

Mine someone's review style:

```bash
review-bot persona create deepam --github-user deepam-kapur
```

This searches accessible repos for the user's PR reviews, analyzes their patterns with Claude, and saves a persona profile. You'll see a summary of what it learned — their tone, top priorities, and pet peeves.

Run a review on a PR:

```bash
review-bot review https://github.com/org/repo/pull/42 --as deepam
```

The bot reviews the diff through that persona's lens and prints the categorized feedback.

Start the webhook server for automatic reviews:

```bash
review-bot server start
```

Now just assign `deepam-bot` as a reviewer on any PR — the bot handles the rest. You can also trigger reviews by commenting `/review-as deepam` on a PR or adding a `review:deepam` label.

---

## Configuration

All environment variables use the `REVIEW_BOT_` prefix and override the config file at `~/.review-bot/config.yaml`.

Three required variables:

```bash
REVIEW_BOT_GITHUB_APP_ID=123456
REVIEW_BOT_PRIVATE_KEY_PATH=~/.review-bot/private-key.pem
REVIEW_BOT_WEBHOOK_SECRET=your-secret-here
```

You can set these in your shell profile, a `.env` file, or pass them directly.

Per-repo settings can be customized with a `.review-like-him.yml` file in the repository root. This lets individual repos override default behavior without changing global config.

Persona profiles live in `~/.review-bot/personas/` as editable YAML files — tweak tone, priorities, or pet peeves to taste. Manual edits are preserved when you re-mine with `review-bot persona update`.

Other useful persona commands:

```bash
review-bot persona list              # list all personas
review-bot persona show deepam       # show full persona details
review-bot persona update deepam     # re-mine with latest reviews
review-bot persona edit deepam       # open in $EDITOR
```

---

<details>
<summary>Architecture</summary>

<br>

When a PR event hits the webhook server, the bot validates the HMAC signature, loads the matching persona profile, and scans the repo for conventions (test frameworks, linting rules, CI setup). It then builds a Claude prompt combining the persona's review style with the diff context. Claude reviews the code through that persona's lens, and the bot posts a categorized review with inline comments as a GitHub PR review.

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

Key design decisions:

- **Single GitHub App** — one app, multiple personas. No separate accounts per reviewer.
- **Claude Code SDK** — uses CLI auth, no API keys to manage.
- **Async throughout** — httpx, aiosqlite, FastAPI for non-blocking I/O.
- **Repo-aware reviews** — the bot scans each repo's conventions before reviewing, so it adapts to context.

</details>

---

## Contributing

```bash
git clone https://github.com/your-org/review-like-him.git
cd review-like-him
pip install -e ".[dev]"
pytest
ruff check . && ruff format .
```

PRs welcome. Clone the repo, install dev dependencies, make sure tests pass and linting is clean.

---

## License

[MIT](LICENSE)

"""Persona management commands: create, list, show, update, edit, mine."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime

import click
import httpx

from review_bot.persona.store import PersonaStore

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine from sync Click context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def _create_mining_progress_handler() -> callable:
    """Create a closure that handles MiningProgress events with rich CLI output.

    Returns a callback function matching Callable[[MiningProgress], None].
    """
    from review_bot.persona.miner import MiningProgress

    # Mutable state tracked across calls
    state = {
        "current_repo": None,
        "current_phase": None,
        "comment_pages": [],
        "pr_pages": [],
        "last_pr_reviews_count": 0,
    }

    def _flush_phase_summary(phase: str, progress: MiningProgress) -> None:
        """Print summary line when transitioning away from a pagination phase."""
        if phase == "fetching_comments" and state["comment_pages"]:
            total_pages = len(state["comment_pages"])
            click.echo(
                f"\r\033[K  Fetching review comments... ({total_pages} pages) "
                f"found {progress.items_found} comments"
            )
            state["comment_pages"] = []
        elif phase == "fetching_prs" and state["pr_pages"]:
            total_pages = len(state["pr_pages"])
            click.echo(
                f"\r\033[K  Fetching pull requests... ({total_pages} pages) "
                f"found {progress.items_found} PRs"
            )
            state["pr_pages"] = []
        elif phase == "fetching_pr_reviews" and state["last_pr_reviews_count"] > 0:
            click.echo(
                f"\r\033[K  \u2713 Found {progress.items_found} matching reviews"
            )
            state["last_pr_reviews_count"] = 0

    def handler(progress: MiningProgress) -> None:
        prev_phase = state["current_phase"]

        # Flush summary of previous phase when transitioning
        if prev_phase and prev_phase != progress.phase:
            _flush_phase_summary(prev_phase, progress)

        state["current_phase"] = progress.phase

        # -- Repo header: print once per new repo --
        if progress.repo and progress.repo != state["current_repo"]:
            state["current_repo"] = progress.repo
            idx = progress.repo_index or "?"
            total = progress.repo_total or "?"
            click.echo(
                f"\n\U0001f4e6 [{idx}/{total}] "
                + click.style(progress.repo, bold=True)
            )

        # -- Phase-specific rendering --
        if progress.phase == "discovering_repos":
            if progress.repo_total is not None:
                click.echo(f"  Found {progress.repo_total} repos with reviews")
            else:
                click.echo("\u23f3 Discovering repos with reviews...")

        elif progress.phase == "fetching_comments":
            if progress.page is not None:
                state["comment_pages"].append(progress.page)
            current_page = state["comment_pages"][-1] if state["comment_pages"] else "?"
            items = progress.items_found or 0
            msg = f"  Fetching review comments... (page {current_page}, {items} items found)"
            sys.stderr.flush()
            click.echo(f"\r\033[K{msg}", nl=False)

        elif progress.phase == "fetching_prs":
            if progress.page is not None:
                state["pr_pages"].append(progress.page)
            current_page = state["pr_pages"][-1] if state["pr_pages"] else "?"
            items = progress.items_found or 0
            msg = f"  Fetching pull requests... (page {current_page}, {items} items found)"
            click.echo(f"\r\033[K{msg}", nl=False)

        elif progress.phase == "fetching_pr_reviews":
            state["last_pr_reviews_count"] = (progress.pr_index or 0)
            pr_idx = progress.pr_index or 0
            pr_total = progress.pr_total or 0
            pr_num = progress.pr_number or "?"
            if pr_total > 0:
                filled = int(pr_idx / pr_total * 8)
                bar = "#" * filled + "-" * (8 - filled)
                msg = (
                    f"  Scanning PR reviews... [{bar}] "
                    f"{pr_idx}/{pr_total}  PR #{pr_num}"
                )
            else:
                msg = f"  Scanning PR reviews...  PR #{pr_num}"
            click.echo(f"\r\033[K{msg}", nl=False)

        elif progress.phase == "done":
            repo_total = progress.repo_total or 0
            click.echo(
                f"\n\u2705 Found {progress.items_found} review comments "
                f"across {repo_total} repos"
            )

    return handler


async def _run_mining(
    github_user: str,
    since: str | None = None,
) -> list[dict]:
    """Shared helper: mine reviews for a GitHub user with rich progress display.

    Sets up the HTTP client, creates the miner, attaches the progress handler,
    and returns the list of mined review dicts.

    Args:
        github_user: GitHub username to mine reviews from.
        since: Optional ISO 8601 timestamp. When provided, only fetch reviews
            created after this date.
    """
    from review_bot.persona.miner import GitHubReviewMiner

    headers = {"Accept": "application/vnd.github+json"}
    gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"

    progress_handler = _create_mining_progress_handler()

    async with httpx.AsyncClient(
        headers=headers,
        timeout=30.0,
    ) as client:
        miner = GitHubReviewMiner(client)
        kwargs: dict = {
            "progress_callback": progress_handler,
        }
        if since is not None:
            kwargs["since"] = since
        try:
            reviews = await miner.mine_user_reviews(github_user, **kwargs)
        except TypeError:
            # Miner may not support 'since' yet — fall back to full mine
            reviews = await miner.mine_user_reviews(
                github_user,
                progress_callback=progress_handler,
            )

    return reviews


def _deduplicate_reviews(
    existing: list[dict],
    new: list[dict],
) -> list[dict]:
    """Merge two review lists, deduplicating by composite key.

    The composite key is (repo, pr_number, comment_body, created_at).

    Args:
        existing: Previously cached reviews.
        new: Newly mined reviews.

    Returns:
        Merged and deduplicated list of review dicts.
    """
    seen: set[tuple] = set()
    merged: list[dict] = []

    for review in existing + new:
        key = (
            review.get("repo", ""),
            review.get("pr_number"),
            review.get("comment_body", ""),
            review.get("created_at", ""),
        )
        if key not in seen:
            seen.add(key)
            merged.append(review)

    return merged


@click.group()
def persona() -> None:
    """Manage reviewer personas."""


@persona.command("create")
@click.argument("name")
@click.option(
    "--github-user",
    required=True,
    help="GitHub username to mine reviews from.",
)
def persona_create(name: str, github_user: str) -> None:
    """Create a new persona by mining a GitHub user's review history."""
    store = PersonaStore()

    if store.exists(name):
        click.echo(
            click.style(f"Persona '{name}' already exists. Use 'update' instead.", fg="yellow")
        )
        return

    click.echo(click.style(f"Mining review history for {github_user}...\n", fg="cyan"))

    async def _create() -> None:
        from review_bot.persona.analyzer import PersonaAnalyzer
        from review_bot.persona.temporal import apply_weights

        reviews = await _run_mining(github_user)

        if not reviews:
            click.echo(click.style("No reviews found for this user.", fg="red"))
            return

        click.echo(f"\nFound {len(reviews)} review comments.")

        # Apply temporal weighting
        click.echo("Applying temporal weighting...")
        weighted = apply_weights(reviews)

        # LLM analysis
        click.echo(click.style("Analyzing review patterns with Claude...", fg="cyan"))
        analyzer = PersonaAnalyzer()
        profile = await analyzer.analyze(weighted, github_user, name)

        # Preview
        click.echo(click.style(f"\n--- Persona Preview: {name} ---\n", fg="green"))
        click.echo(f"  Tone: {profile.tone}")
        click.echo(f"  Mined from: {profile.mined_from}")
        click.echo(f"  Priorities: {len(profile.priorities)}")
        click.echo(f"  Pet peeves: {len(profile.pet_peeves)}")
        if profile.pet_peeves:
            for peeve in profile.pet_peeves[:3]:
                click.echo(f"    - {peeve}")
        click.echo()

        # Save
        store.save(profile)
        click.echo(click.style(f"\u2713 Persona '{name}' saved.", fg="green", bold=True))

    try:
        _run_async(_create())
    except Exception as exc:
        click.echo(click.style(f"Error: {exc}", fg="red"))
        raise SystemExit(1) from exc


@persona.command("list")
def persona_list() -> None:
    """List all saved personas."""
    store = PersonaStore()
    profiles = store.list_all()

    if not profiles:
        click.echo("No personas found. Create one with: review-bot persona create")
        return

    # Table header
    header = f"{'Name':<15} {'GitHub User':<20} {'Comments':<15} {'Repos':<8} {'Updated':<12}"
    click.echo(click.style(header, bold=True))
    click.echo("\u2500" * len(header))

    for p in profiles:
        # Parse mined_from for stats
        comments = ""
        repos = ""
        if p.mined_from:
            parts = p.mined_from.split()
            if len(parts) >= 1:
                comments = parts[0]
            for i, word in enumerate(parts):
                if word == "across" and i + 1 < len(parts):
                    repos = parts[i + 1]
                    break

        click.echo(
            f"{p.name:<15} {p.github_user:<20} {comments:<15} {repos:<8} {p.last_updated:<12}"
        )


@persona.command("show")
@click.argument("name")
def persona_show(name: str) -> None:
    """Display a full persona profile."""
    store = PersonaStore()

    try:
        profile = store.load(name)
    except FileNotFoundError:
        click.echo(click.style(f"Persona '{name}' not found.", fg="red"))
        raise SystemExit(1)

    header = f"\n\u2550\u2550\u2550 Persona: {profile.name} \u2550\u2550\u2550\n"
    click.echo(click.style(header, fg="cyan", bold=True))
    click.echo(f"  GitHub User:  {profile.github_user}")
    click.echo(f"  Mined From:   {profile.mined_from}")
    click.echo(f"  Last Updated: {profile.last_updated}")
    click.echo(f"  Tone:         {profile.tone}")

    if profile.priorities:
        click.echo(click.style("\n  Priorities:", bold=True))
        for p in profile.priorities:
            severity_color = {
                "critical": "red",
                "strict": "yellow",
                "moderate": "cyan",
                "opinionated": "white",
            }.get(p.severity, "white")
            click.echo(
                f"    [{click.style(p.severity, fg=severity_color)}] "
                f"{p.category}: {p.description}"
            )

    if profile.pet_peeves:
        click.echo(click.style("\n  Pet Peeves:", bold=True))
        for peeve in profile.pet_peeves:
            click.echo(f"    - {peeve}")

    sp = profile.severity_pattern
    click.echo(click.style("\n  Severity Pattern:", bold=True))
    if sp.blocks_on:
        click.echo(click.style("    Blocks on:", fg="red"))
        for item in sp.blocks_on:
            click.echo(f"      - {item}")
    if sp.nits_on:
        click.echo(click.style("    Nits on:", fg="yellow"))
        for item in sp.nits_on:
            click.echo(f"      - {item}")
    if sp.approves_when:
        click.echo(click.style("    Approves when:", fg="green"))
        click.echo(f"      {sp.approves_when}")

    if profile.overrides:
        click.echo(click.style("\n  Overrides:", bold=True))
        for override in profile.overrides:
            click.echo(f"    - {override}")

    click.echo()


@persona.command("update")
@click.argument("name")
def persona_update(name: str) -> None:
    """Re-mine and update an existing persona with latest reviews."""
    store = PersonaStore()

    try:
        existing = store.load(name)
    except FileNotFoundError:
        click.echo(click.style(f"Persona '{name}' not found.", fg="red"))
        raise SystemExit(1)

    click.echo(
        click.style(f"Updating persona '{name}' (user: {existing.github_user})...\n", fg="cyan")
    )

    async def _update() -> None:
        from review_bot.persona.analyzer import PersonaAnalyzer
        from review_bot.persona.temporal import apply_weights

        reviews = await _run_mining(existing.github_user)

        if not reviews:
            click.echo(click.style("No reviews found.", fg="red"))
            return

        click.echo(f"\nFound {len(reviews)} review comments.")

        weighted = apply_weights(reviews)

        click.echo(click.style("Re-analyzing review patterns...", fg="cyan"))
        analyzer = PersonaAnalyzer()
        profile = await analyzer.analyze(weighted, existing.github_user, name)

        # Preserve manual overrides from existing persona
        profile.overrides = existing.overrides

        store.save(profile)
        click.echo(click.style(f"\u2713 Persona '{name}' updated.", fg="green", bold=True))

    try:
        _run_async(_update())
    except Exception as exc:
        click.echo(click.style(f"Error: {exc}", fg="red"))
        raise SystemExit(1) from exc


@persona.command("mine")
@click.argument("name")
@click.option(
    "--github-user",
    default=None,
    help="GitHub username to mine reviews from (required for first mine).",
)
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help="Force a full re-mine, ignoring cached data.",
)
def persona_mine(name: str, github_user: str | None, full: bool) -> None:
    """Mine reviews for a persona (incremental by default)."""
    store = PersonaStore()
    existing = None

    if store.exists(name):
        existing = store.load(name)
        if not github_user:
            github_user = existing.github_user

    if not github_user:
        click.echo(
            click.style(
                "No existing persona found. --github-user is required for first mine.",
                fg="red",
            )
        )
        raise SystemExit(1)

    async def _mine() -> None:
        from review_bot.persona.analyzer import PersonaAnalyzer
        from review_bot.persona.dedup import collapse_threads
        from review_bot.persona.temporal import apply_weights

        since_val: str | None = None
        is_incremental = False

        if existing and existing.last_mined_at and not full:
            # Attempt incremental mining
            try:
                dt = datetime.fromisoformat(existing.last_mined_at)
                # Normalize timezone: if naive, assume UTC
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                else:
                    dt = dt.astimezone(UTC)
                since_val = dt.isoformat()
                is_incremental = True
            except ValueError:
                logger.warning(
                    "Could not parse last_mined_at='%s', falling back to full mine",
                    existing.last_mined_at,
                )

        if is_incremental:
            click.echo(
                click.style(
                    f"Incremental mine for '{name}' (since {since_val})...\n",
                    fg="cyan",
                )
            )
        else:
            click.echo(
                click.style(
                    f"Full mine for '{name}' (user: {github_user})...\n",
                    fg="cyan",
                )
            )

        new_reviews = await _run_mining(github_user, since=since_val)

        if is_incremental:
            cached_reviews = store.load_reviews(name)
            all_reviews = _deduplicate_reviews(cached_reviews, new_reviews)
            click.echo(
                f"\nNew reviews: {len(new_reviews)}, "
                f"Total (after dedup): {len(all_reviews)}"
            )
        else:
            all_reviews = new_reviews
            click.echo(f"\nFound {len(all_reviews)} review comments.")

        if not all_reviews:
            click.echo(click.style("No reviews found.", fg="red"))
            return

        # Save raw reviews cache
        store.save_reviews(name, all_reviews)

        # Apply dedup threading and temporal weighting
        deduped = collapse_threads(list(all_reviews), github_user)
        weighted = apply_weights(deduped)

        # Analyze
        analyzer = PersonaAnalyzer()

        if is_incremental and existing:
            click.echo(
                click.style("Re-analyzing review patterns...", fg="cyan")
            )
            # Weight only the new reviews for the incremental arg
            new_deduped = collapse_threads(list(new_reviews), github_user)
            new_weighted = apply_weights(new_deduped)
            profile = await analyzer.analyze_incremental(
                existing, new_weighted, weighted,
            )
        else:
            click.echo(
                click.style(
                    "Analyzing review patterns with Claude...", fg="cyan",
                )
            )
            profile = await analyzer.analyze(weighted, github_user, name)
            profile.last_mined_at = datetime.now(UTC).isoformat()

        store.save(profile)
        click.echo(
            click.style(f"\u2713 Persona '{name}' saved.", fg="green", bold=True)
        )

    try:
        _run_async(_mine())
    except Exception as exc:
        click.echo(click.style(f"Error: {exc}", fg="red"))
        raise SystemExit(1) from exc


@persona.command("delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def persona_delete(name: str, yes: bool) -> None:
    """Delete a persona profile."""
    store = PersonaStore()

    if not store.exists(name):
        click.echo(click.style(f"Persona '{name}' not found.", fg="red"))
        raise SystemExit(1)

    if not yes:
        if not click.confirm(f"Delete persona '{name}'? This cannot be undone"):
            click.echo("Cancelled.")
            return

    try:
        store.delete(name)
        click.echo(click.style(f"✓ Persona '{name}' deleted.", fg="green"))
    except Exception as exc:
        click.echo(click.style(f"Error deleting persona: {exc}", fg="red"))
        raise SystemExit(1) from exc


@persona.command("edit")
@click.argument("name")
def persona_edit(name: str) -> None:
    """Open a persona's YAML file in $EDITOR."""
    store = PersonaStore()

    if not store.exists(name):
        click.echo(click.style(f"Persona '{name}' not found.", fg="red"))
        raise SystemExit(1)

    from pathlib import Path

    personas_dir = Path.home() / ".review-bot" / "personas"
    filepath = personas_dir / f"{name}.yaml"
    editor = os.environ.get("EDITOR", "vi")

    click.echo(f"Opening {filepath} in {editor}...")

    try:
        subprocess.run([editor, str(filepath)], check=True)
    except subprocess.CalledProcessError as exc:
        click.echo(click.style(f"Editor exited with error: {exc}", fg="red"))
        raise SystemExit(1) from exc
    except FileNotFoundError:
        click.echo(
            click.style(
                f"Editor '{editor}' not found. Set $EDITOR to your preferred editor.",
                fg="red",
            )
        )
        raise SystemExit(1)

    # Validate the edited file
    try:
        store.load(name)
        click.echo(click.style(f"\u2713 Persona '{name}' validated.", fg="green"))
    except Exception as exc:
        click.echo(click.style(f"Warning: YAML validation failed: {exc}", fg="yellow"))

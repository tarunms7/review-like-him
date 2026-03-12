"""Persona management commands: create, list, show, update, edit."""

from __future__ import annotations

import asyncio
import os
import subprocess

import click
import httpx

from review_bot.persona.store import PersonaStore


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
        from review_bot.persona.miner import GitHubReviewMiner
        from review_bot.persona.temporal import apply_weights

        async with httpx.AsyncClient(
            headers={"Accept": "application/vnd.github+json"},
            timeout=30.0,
        ) as client:
            miner = GitHubReviewMiner(client)

            # Progress tracking
            with click.progressbar(
                length=100,
                label="Mining repos",
                show_pos=True,
            ) as bar:
                current_total = [0]

                def progress_cb(repo_name: str, current: int, total: int) -> None:
                    if current_total[0] != total:
                        bar.length = total
                        current_total[0] = total
                    bar.update(1)
                    bar.label = f"Mining {repo_name}"

                reviews = await miner.mine_user_reviews(
                    github_user,
                    progress_callback=progress_cb,
                )

                # Ensure bar completes
                remaining = bar.length - bar.pos
                if remaining > 0:
                    bar.update(remaining)

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
        click.echo(click.style(f"✓ Persona '{name}' saved.", fg="green", bold=True))

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
    click.echo("─" * len(header))

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

    click.echo(click.style(f"\n═══ Persona: {profile.name} ═══\n", fg="cyan", bold=True))
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
        from review_bot.persona.miner import GitHubReviewMiner
        from review_bot.persona.temporal import apply_weights

        async with httpx.AsyncClient(
            headers={"Accept": "application/vnd.github+json"},
            timeout=30.0,
        ) as client:
            miner = GitHubReviewMiner(client)

            with click.progressbar(
                length=100,
                label="Mining repos",
                show_pos=True,
            ) as bar:
                current_total = [0]

                def progress_cb(repo_name: str, current: int, total: int) -> None:
                    if current_total[0] != total:
                        bar.length = total
                        current_total[0] = total
                    bar.update(1)
                    bar.label = f"Mining {repo_name}"

                reviews = await miner.mine_user_reviews(
                    existing.github_user,
                    progress_callback=progress_cb,
                )

                remaining = bar.length - bar.pos
                if remaining > 0:
                    bar.update(remaining)

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
        click.echo(click.style(f"✓ Persona '{name}' updated.", fg="green", bold=True))

    try:
        _run_async(_update())
    except Exception as exc:
        click.echo(click.style(f"Error: {exc}", fg="red"))
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
        click.echo(click.style(f"✓ Persona '{name}' validated.", fg="green"))
    except Exception as exc:
        click.echo(click.style(f"Warning: YAML validation failed: {exc}", fg="yellow"))

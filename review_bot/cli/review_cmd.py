"""Manual review trigger command."""

from __future__ import annotations

import asyncio

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


@click.command()
@click.argument("pr_url")
@click.option(
    "--as",
    "persona_name",
    required=True,
    help="Persona name to review as.",
)
def review_cmd(pr_url: str, persona_name: str) -> None:
    """Run a review on a GitHub PR URL as a given persona."""
    store = PersonaStore()

    # Validate persona exists
    if not store.exists(persona_name):
        click.echo(click.style(f"Persona '{persona_name}' not found.", fg="red"))
        click.echo("Available personas:")
        for p in store.list_all():
            click.echo(f"  - {p.name}")
        raise SystemExit(1)

    click.echo(
        click.style(
            f"Reviewing {pr_url} as {persona_name}-bot...\n",
            fg="cyan",
        )
    )

    async def _review():
        from review_bot.github.api import GitHubAPIClient
        from review_bot.review.orchestrator import ReviewOrchestrator

        async with httpx.AsyncClient(
            headers={"Accept": "application/vnd.github+json"},
            timeout=30.0,
        ) as client:
            github_client = GitHubAPIClient(client)
            orchestrator = ReviewOrchestrator(github_client, store)
            return await orchestrator.run_review_from_url(pr_url, persona_name)

    try:
        result = _run_async(_review())
    except ValueError as exc:
        click.echo(click.style(f"Invalid PR URL: {exc}", fg="red"))
        raise SystemExit(1) from exc
    except Exception as exc:
        click.echo(click.style(f"Review failed: {exc}", fg="red"))
        raise SystemExit(1) from exc

    # Display results
    _display_result(result)


def _display_result(result) -> None:
    """Pretty-print a ReviewResult to the terminal."""
    verdict_colors = {
        "approve": "green",
        "request_changes": "red",
        "comment": "yellow",
    }
    verdict_color = verdict_colors.get(result.verdict, "white")

    click.echo(click.style("\n═══ Review Result ═══\n", fg="cyan", bold=True))
    click.echo(f"  PR:       {result.pr_url}")
    click.echo(f"  Persona:  {result.persona_name}-bot")
    click.echo(
        f"  Verdict:  {click.style(result.verdict.upper(), fg=verdict_color, bold=True)}"
    )

    if result.summary_sections:
        click.echo(click.style("\n  Summary:", bold=True))
        for section in result.summary_sections:
            click.echo(f"\n  {section.emoji} {click.style(section.title, bold=True)}")
            for finding in section.findings:
                click.echo(f"    • {finding}")

    if result.inline_comments:
        click.echo(click.style(f"\n  Inline Comments ({len(result.inline_comments)}):", bold=True))
        for comment in result.inline_comments:
            click.echo(
                f"    {click.style(comment.file, fg='cyan')}:{comment.line}"
            )
            click.echo(f"      {comment.body}")

    click.echo()

"""Manual review trigger command."""

from __future__ import annotations

import click
from rich.console import Console

from review_bot.cli.utils import _run_async, create_github_client, get_github_token
from review_bot.persona.store import PersonaStore

console = Console()


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
        from sqlalchemy.ext.asyncio import create_async_engine

        from review_bot.config.paths import ensure_directories
        from review_bot.config.settings import Settings
        from review_bot.github.api import GitHubAPIClient
        from review_bot.review.orchestrator import ReviewOrchestrator

        settings = Settings()
        ensure_directories()

        gh_token = get_github_token()
        if not gh_token:
            click.echo(
                click.style(
                    "Warning: No GITHUB_TOKEN or GH_TOKEN set. "
                    "Private repos will not be accessible.",
                    fg="yellow",
                )
            )

        # Create database engine for logging reviews
        engine = create_async_engine(settings.db_url, echo=False)

        try:
            # Initialize database tables
            with console.status("[cyan]Initializing database...", spinner="dots"):
                from review_bot.server.app import _init_database

                await _init_database(engine)

            with console.status("[cyan]Running AI review...", spinner="dots"):
                async with create_github_client(gh_token) as client:
                    github_client = GitHubAPIClient(client)
                    orchestrator = ReviewOrchestrator(
                        github_client,
                        store,
                        db_engine=engine,
                    )
                    return await orchestrator.run_review_from_url(pr_url, persona_name)
        finally:
            await engine.dispose()

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

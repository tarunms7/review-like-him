"""CLI command for querying server status and rate limit state."""

from __future__ import annotations

import click
import httpx
from rich.console import Console
from rich.table import Table

from review_bot.config.settings import Settings


@click.command()
def status_cmd() -> None:
    """Show current GitHub API rate limit state from the running server."""
    settings = Settings()
    status_url = f"http://localhost:{settings.port}/status"

    try:
        response = httpx.get(status_url, timeout=5.0)
        response.raise_for_status()
    except httpx.ConnectError:
        click.echo(
            click.style(
                f"Could not connect to review-bot server at localhost:{settings.port}. "
                "Is it running?",
                fg="red",
            )
        )
        raise SystemExit(1)
    except httpx.HTTPStatusError as exc:
        click.echo(click.style(f"Server returned error: {exc.response.status_code}", fg="red"))
        raise SystemExit(1) from exc
    except httpx.RequestError as exc:
        click.echo(click.style(f"Request failed: {exc}", fg="red"))
        raise SystemExit(1) from exc

    data = response.json()
    status = data.get("status", "unknown")

    if status == "degraded":
        reason = data.get("reason", "Unknown reason")
        click.echo(click.style(f"Server status: degraded — {reason}", fg="yellow"))
        # Continue to show available data instead of returning early

    rate_limits: dict = data.get("rate_limits", {})

    if not rate_limits:
        click.echo(click.style(f"Server status: {status}", fg="green"))
        click.echo("No rate limit data recorded yet.")
        return

    console = Console()
    table = Table(title=f"GitHub API Rate Limits  (status: {status})")
    table.add_column("Resource", style="cyan", no_wrap=True)
    table.add_column("Remaining", justify="right")
    table.add_column("Limit", justify="right")
    table.add_column("Used", justify="right")
    table.add_column("Reset", justify="right")

    for resource in ("core", "search", "graphql"):
        if resource not in rate_limits:
            continue
        info = rate_limits[resource]
        table.add_row(
            resource,
            str(info.get("remaining", "—")),
            str(info.get("limit", "—")),
            str(info.get("used", "—")),
            str(info.get("reset", "—")),
        )

    console.print(table)

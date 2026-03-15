"""CLI command: compare a PR across multiple personas side-by-side."""

from __future__ import annotations

import asyncio
import json
import os
import re

import click

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


_PR_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)")


@click.command()
@click.argument("pr_url")
@click.option(
    "--personas",
    "-p",
    required=True,
    help="Comma-separated persona names (at least 2).",
)
@click.option(
    "--timeout",
    "-t",
    default=120.0,
    type=float,
    help="Timeout per persona in seconds.",
)
@click.option(
    "--json-output",
    is_flag=True,
    default=False,
    help="Output results as JSON.",
)
def compare_cmd(pr_url: str, personas: str, timeout: float, json_output: bool) -> None:
    """Compare a PR review across multiple personas side-by-side."""
    # Parse and validate PR URL
    match = _PR_URL_RE.match(pr_url)
    if not match:
        click.echo(click.style(f"Invalid GitHub PR URL: {pr_url}", fg="red"))
        raise SystemExit(1)

    owner, repo, pr_number = match.group(1), match.group(2), int(match.group(3))

    # Parse persona names
    persona_names = [p.strip() for p in personas.split(",") if p.strip()]
    if len(persona_names) < 2:
        click.echo(click.style("At least 2 personas are required for comparison.", fg="red"))
        raise SystemExit(1)

    # Validate GitHub token
    gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not gh_token:
        click.echo(
            click.style(
                "Warning: No GITHUB_TOKEN or GH_TOKEN set. "
                "Private repos will not be accessible.",
                fg="yellow",
            )
        )

    click.echo(
        click.style(
            f"Comparing {pr_url} across {len(persona_names)} personas...\n",
            fg="cyan",
        )
    )

    async def _compare():
        import httpx

        from review_bot.github.api import GitHubAPIClient
        from review_bot.review.comparator import PersonaComparator

        headers = {"Accept": "application/vnd.github+json"}
        if gh_token:
            headers["Authorization"] = f"Bearer {gh_token}"

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            github_client = GitHubAPIClient(client)
            store = PersonaStore()
            comparator = PersonaComparator(github_client, store)
            return await comparator.compare(
                owner,
                repo,
                pr_number,
                persona_names,
                timeout_per_persona=timeout,
            )

    try:
        result = _run_async(_compare())
    except Exception as exc:
        click.echo(click.style(f"Comparison failed: {exc}", fg="red"))
        raise SystemExit(1) from exc

    # Output results
    if json_output:
        from review_bot.review.comparison_formatter import format_comparison_api

        click.echo(json.dumps(format_comparison_api(result), indent=2))
    else:
        from review_bot.review.comparison_formatter import format_comparison_cli

        click.echo(format_comparison_cli(result))

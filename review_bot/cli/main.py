"""Click command group entry point for review-bot CLI."""

import click

from review_bot.cli.init_cmd import init_cmd
from review_bot.cli.persona_cmd import persona
from review_bot.cli.review_cmd import review_cmd
from review_bot.cli.server_cmd import server
from review_bot.cli.status_cmd import status_cmd


@click.group()
@click.version_option(package_name="review-bot")
def cli() -> None:
    """review-bot — AI code reviewer that mimics real reviewers."""


cli.add_command(init_cmd, name="init")
cli.add_command(persona)
cli.add_command(review_cmd, name="review")
cli.add_command(server)
cli.add_command(status_cmd, name="status")

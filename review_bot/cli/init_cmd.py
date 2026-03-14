"""Interactive setup wizard for review-bot."""

from __future__ import annotations

import shutil
import subprocess

import click
import yaml

from review_bot.config.paths import CONFIG_DIR, CONFIG_FILE, ensure_directories

# Timeout in seconds for subprocess calls
_SUBPROCESS_TIMEOUT = 15


def _detect_webhook_url() -> str | None:
    """Try to auto-detect a public URL from ngrok."""
    try:
        import httpx

        resp = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=2)
        if resp.status_code == 200:
            tunnels = resp.json().get("tunnels", [])
            for t in tunnels:
                public_url = t.get("public_url", "")
                if public_url.startswith("https://"):
                    return public_url
    except Exception:
        pass
    return None


def _check_claude_cli() -> bool:
    """Check if the claude CLI is installed and accessible."""
    return shutil.which("claude") is not None


@click.command()
def init_cmd() -> None:
    """Interactive setup wizard for review-bot."""
    click.echo(click.style("\n🤖 review-bot Setup Wizard\n", fg="cyan", bold=True))

    # Ensure directories exist first
    try:
        ensure_directories()
    except OSError as exc:
        click.echo(click.style(f"Failed to create config directories: {exc}", fg="red"))
        click.echo(f"Ensure you have write permissions to {CONFIG_DIR}")
        raise SystemExit(1) from exc

    # Step 1: Check claude CLI
    click.echo("Checking prerequisites...")
    if _check_claude_cli():
        click.echo(click.style("  ✓ claude CLI found", fg="green"))
    else:
        click.echo(click.style("  ✗ claude CLI not found", fg="red"))
        click.echo("    Install it from https://claude.ai/download")
        click.echo("    The claude CLI is required for AI-powered code reviews.")
        if not click.confirm("Continue anyway?", default=False):
            raise SystemExit(1)

    # Verify claude is logged in
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        if result.returncode == 0:
            click.echo(
                click.style(f"  ✓ claude CLI version: {result.stdout.strip()}", fg="green")
            )
        else:
            click.echo(
                click.style(
                    f"  ⚠ claude CLI returned exit code {result.returncode}",
                    fg="yellow",
                )
            )
            if result.stderr.strip():
                click.echo(f"    stderr: {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        click.echo(
            click.style(
                f"  ⚠ claude CLI version check timed out after {_SUBPROCESS_TIMEOUT}s",
                fg="yellow",
            )
        )
    except FileNotFoundError:
        click.echo(click.style("  ⚠ claude CLI not found in PATH", fg="yellow"))
    except Exception as exc:
        click.echo(click.style(f"  ⚠ Could not verify claude CLI: {exc}", fg="yellow"))

    # Step 2: GitHub App setup
    click.echo(click.style("\n--- GitHub App Setup ---\n", fg="cyan"))

    from review_bot.github.setup import guide_app_creation

    app_info = guide_app_creation()

    # Step 3: Webhook URL
    click.echo(click.style("\n--- Webhook URL ---\n", fg="cyan"))
    detected_url = _detect_webhook_url()
    if detected_url:
        click.echo(f"  Detected ngrok URL: {detected_url}")
        webhook_url = click.prompt(
            "Webhook URL",
            default=f"{detected_url}/webhook",
        )
    else:
        webhook_url = click.prompt(
            "Webhook URL (e.g. https://your-domain.com/webhook)"
        )

    # Step 4: Save config
    config = {
        "github_app_id": app_info["app_id"],
        "private_key_path": app_info["private_key_path"],
        "webhook_secret": app_info["webhook_secret"],
        "webhook_url": webhook_url,
    }

    CONFIG_FILE.write_text(
        yaml.dump(config, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    click.echo(click.style(f"\n  ✓ Config saved to {CONFIG_FILE}", fg="green"))
    click.echo(click.style("\n✓ Setup complete.", fg="green", bold=True))
    click.echo(f"\nNext steps:")
    click.echo(f"  1. Create a persona: review-bot persona create <name> --github-user <user>")
    click.echo(f"  2. Start the server: review-bot server start")

"""Interactive setup wizard for review-bot."""

from __future__ import annotations

import shutil
import subprocess

import click
import yaml

from review_bot.config.paths import CONFIG_DIR, CONFIG_FILE


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

    # Step 1: Check claude CLI
    click.echo("Checking prerequisites...")
    if _check_claude_cli():
        click.echo(click.style("  ✓ claude CLI found", fg="green"))
    else:
        click.echo(click.style("  ✗ claude CLI not found", fg="red"))
        click.echo("    Install it from https://claude.ai/download")
        if not click.confirm("Continue anyway?", default=False):
            raise SystemExit(1)

    # Verify claude is logged in
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            click.echo(
                click.style(f"  ✓ claude CLI version: {result.stdout.strip()}", fg="green")
            )
    except Exception:
        click.echo(click.style("  ⚠ Could not verify claude CLI", fg="yellow"))

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
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
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

"""Server management commands: start, status, logs."""

from __future__ import annotations

import click

from review_bot.config.paths import DB_PATH
from review_bot.config.settings import Settings


@click.group()
def server() -> None:
    """Manage the review-bot webhook server."""


@server.command("start")
@click.option("--host", default=None, help="Bind host (default: 0.0.0.0).")
@click.option("--port", default=None, type=int, help="Bind port (default: 8000).")
@click.option("--daemon", is_flag=True, help="Run as background daemon.")
def server_start(host: str | None, port: int | None, daemon: bool) -> None:
    """Start the webhook listener server."""
    settings = Settings()
    bind_host = host or settings.host
    bind_port = port or settings.port

    if daemon:
        _start_daemon(bind_host, bind_port)
    else:
        _start_foreground(bind_host, bind_port)


def _start_foreground(host: str, port: int) -> None:
    """Start uvicorn in the foreground."""
    click.echo(
        click.style(f"Starting review-bot server on {host}:{port}...", fg="cyan")
    )
    try:
        import uvicorn

        uvicorn.run(
            "review_bot.server.app:create_app",
            host=host,
            port=port,
            factory=True,
            log_level="info",
        )
    except ImportError:
        click.echo(click.style("Error: uvicorn not installed.", fg="red"))
        click.echo("Install it with: pip install uvicorn")
        raise SystemExit(1)
    except Exception as exc:
        click.echo(click.style(f"Server error: {exc}", fg="red"))
        raise SystemExit(1) from exc


def _start_daemon(host: str, port: int) -> None:
    """Start the server as a background process."""
    import subprocess
    import sys

    click.echo(
        click.style(f"Starting review-bot daemon on {host}:{port}...", fg="cyan")
    )

    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "review_bot.server.app:create_app",
                "--host",
                host,
                "--port",
                str(port),
                "--factory",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        click.echo(click.style(f"✓ Daemon started (PID: {proc.pid})", fg="green"))
    except Exception as exc:
        click.echo(click.style(f"Failed to start daemon: {exc}", fg="red"))
        raise SystemExit(1) from exc


@server.command("status")
def server_status() -> None:
    """Show server status and recent review activity."""
    from review_bot.persona.store import PersonaStore

    # Check personas
    store = PersonaStore()
    personas = store.list_all()
    click.echo(click.style("\n═══ review-bot Status ═══\n", fg="cyan", bold=True))
    click.echo(f"  Active personas: {len(personas)}")
    for p in personas:
        click.echo(f"    - {p.name} ({p.github_user})")

    # Check DB for recent reviews
    if DB_PATH.exists():
        try:
            import sqlite3

            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.execute(
                "SELECT COUNT(*) FROM reviews"
            )
            total = cursor.fetchone()[0]
            click.echo(f"\n  Total reviews: {total}")

            cursor = conn.execute(
                "SELECT persona_name, repo, pr_number, verdict, created_at "
                "FROM reviews ORDER BY created_at DESC LIMIT 5"
            )
            rows = cursor.fetchall()
            if rows:
                click.echo(click.style("\n  Recent reviews:", bold=True))
                for row in rows:
                    persona_name, repo, pr_num, verdict, created = row
                    verdict_color = {
                        "approve": "green",
                        "request_changes": "red",
                        "comment": "yellow",
                    }.get(verdict, "white")
                    click.echo(
                        f"    {created[:16]}  {repo}#{pr_num}  "
                        f"as {persona_name}  "
                        f"{click.style(verdict, fg=verdict_color)}"
                    )
            conn.close()
        except Exception:
            click.echo("  Database: not initialized")
    else:
        click.echo("\n  Database: not yet created")

    click.echo()


@server.command("logs")
@click.option("-n", "--lines", default=20, help="Number of recent entries to show.")
def server_logs(lines: int) -> None:
    """Tail recent review activity from the database."""
    if not DB_PATH.exists():
        click.echo(click.style("No database found. Run a review first.", fg="yellow"))
        return

    try:
        import sqlite3

        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.execute(
            "SELECT persona_name, repo, pr_number, pr_url, verdict, "
            "comment_count, duration_ms, created_at "
            "FROM reviews ORDER BY created_at DESC LIMIT ?",
            (lines,),
        )
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            click.echo("No review activity yet.")
            return

        click.echo(click.style("\n═══ Recent Review Activity ═══\n", fg="cyan", bold=True))

        for row in rows:
            persona_name, repo, pr_num, pr_url, verdict, comments, duration_ms, created = row
            verdict_color = {
                "approve": "green",
                "request_changes": "red",
                "comment": "yellow",
            }.get(verdict, "white")

            duration_s = (duration_ms or 0) / 1000
            click.echo(
                f"  {created[:19]}  "
                f"{click.style(persona_name, bold=True)} → "
                f"{repo}#{pr_num}  "
                f"{click.style(verdict, fg=verdict_color)}  "
                f"({comments} comments, {duration_s:.1f}s)"
            )

        click.echo()

    except Exception as exc:
        click.echo(click.style(f"Error reading logs: {exc}", fg="red"))

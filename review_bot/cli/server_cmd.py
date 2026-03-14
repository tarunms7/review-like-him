"""Server management commands: start, stop, restart, status, logs."""

from __future__ import annotations

import os
import signal

import click

from review_bot.config.paths import DB_PATH, LOG_DIR, PID_FILE, ensure_directories
from review_bot.config.settings import Settings


def _read_pid() -> int | None:
    """Read the daemon PID from the PID file."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        # Check if process is actually running
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        # Stale PID file — clean up
        PID_FILE.unlink(missing_ok=True)
        return None


def _write_pid(pid: int) -> None:
    """Write a PID to the PID file."""
    ensure_directories()
    PID_FILE.write_text(str(pid), encoding="utf-8")


def _remove_pid() -> None:
    """Remove the PID file."""
    PID_FILE.unlink(missing_ok=True)


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
    settings = Settings()

    # Validate server config
    errors = settings.validate_for_server()
    if errors:
        click.echo(click.style("Server configuration errors:", fg="red"))
        for err in errors:
            click.echo(click.style(f"  • {err}", fg="red"))
        raise SystemExit(1)

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

    # Check if already running
    existing_pid = _read_pid()
    if existing_pid is not None:
        click.echo(
            click.style(f"Server already running (PID: {existing_pid})", fg="yellow")
        )
        click.echo("Use 'review-bot server stop' first, or 'review-bot server restart'.")
        return

    ensure_directories()

    click.echo(
        click.style(f"Starting review-bot daemon on {host}:{port}...", fg="cyan")
    )

    # Write stdout/stderr to log file
    log_file = LOG_DIR / "server.log"

    try:
        log_fd = open(log_file, "a")  # noqa: SIM115
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
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
        )
        _write_pid(proc.pid)
        click.echo(click.style(f"✓ Daemon started (PID: {proc.pid})", fg="green"))
        click.echo(f"  Logs: {log_file}")
    except Exception as exc:
        click.echo(click.style(f"Failed to start daemon: {exc}", fg="red"))
        raise SystemExit(1) from exc


@server.command("stop")
def server_stop() -> None:
    """Stop the running webhook server daemon."""
    pid = _read_pid()
    if pid is None:
        click.echo(click.style("No running server found.", fg="yellow"))
        return

    click.echo(f"Stopping server (PID: {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        _remove_pid()
        click.echo(click.style("✓ Server stopped.", fg="green"))
    except ProcessLookupError:
        _remove_pid()
        click.echo(click.style("Server was not running (stale PID file cleaned up).", fg="yellow"))
    except PermissionError:
        click.echo(click.style(f"Permission denied stopping PID {pid}.", fg="red"))
        raise SystemExit(1)


@server.command("restart")
@click.option("--host", default=None, help="Bind host (default: 0.0.0.0).")
@click.option("--port", default=None, type=int, help="Bind port (default: 8000).")
@click.pass_context
def server_restart(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Restart the webhook server daemon."""
    # Stop if running
    pid = _read_pid()
    if pid is not None:
        click.echo(f"Stopping server (PID: {pid})...")
        try:
            os.kill(pid, signal.SIGTERM)
            _remove_pid()
            click.echo(click.style("✓ Server stopped.", fg="green"))
        except ProcessLookupError:
            _remove_pid()

    # Start as daemon
    settings = Settings()
    bind_host = host or settings.host
    bind_port = port or settings.port
    _start_daemon(bind_host, bind_port)


@server.command("status")
def server_status() -> None:
    """Show server status and recent review activity."""
    import socket

    from review_bot.persona.store import PersonaStore

    settings = Settings()

    # Check running state via PID file and port probe
    pid = _read_pid()
    running = False
    if pid is not None:
        running = True
    else:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                result = sock.connect_ex(("127.0.0.1", settings.port))
                running = result == 0
        except OSError:
            running = False

    click.echo(click.style("\n═══ review-bot Status ═══\n", fg="cyan", bold=True))

    if running:
        pid_info = f" (PID: {pid})" if pid else ""
        click.echo(click.style(f"  Server: running on port {settings.port}{pid_info}", fg="green"))
    else:
        click.echo(click.style(f"  Server: not running (port {settings.port})", fg="red"))

    # Check personas
    store = PersonaStore()
    personas = store.list_all()
    click.echo(f"  Active personas: {len(personas)}")
    for p in personas:
        click.echo(f"    - {p.name} ({p.github_user})")

    # Check DB for recent reviews
    if DB_PATH.exists():
        try:
            import sqlite3

            conn = sqlite3.connect(str(DB_PATH))
            try:
                cursor = conn.execute("SELECT COUNT(*) FROM reviews")
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
            finally:
                conn.close()
        except Exception:
            click.echo("  Database: not initialized")
    else:
        click.echo("\n  Database: not yet created")

    click.echo()


@server.command("logs")
@click.option("-n", "--lines", default=20, help="Number of recent entries to show.")
@click.option("--daemon-log", is_flag=True, help="Show daemon stdout/stderr log instead.")
def server_logs(lines: int, daemon_log: bool) -> None:
    """Tail recent review activity from the database."""
    if daemon_log:
        log_file = LOG_DIR / "server.log"
        if not log_file.exists():
            click.echo(click.style("No daemon log found.", fg="yellow"))
            return

        text = log_file.read_text(encoding="utf-8", errors="replace")
        output_lines = text.splitlines()
        tail = output_lines[-lines:] if len(output_lines) > lines else output_lines
        for line in tail:
            click.echo(line)
        return

    if not DB_PATH.exists():
        click.echo(click.style("No database found. Run a review first.", fg="yellow"))
        return

    try:
        import sqlite3

        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.execute(
                "SELECT persona_name, repo, pr_number, pr_url, verdict, "
                "comment_count, duration_ms, created_at "
                "FROM reviews ORDER BY created_at DESC LIMIT ?",
                (lines,),
            )
            rows = cursor.fetchall()
        finally:
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

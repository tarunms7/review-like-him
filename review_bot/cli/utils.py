"""Shared CLI utilities for review-bot commands."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx


def _run_async(coro) -> Any:
    """Run an async coroutine from sync Click context.

    Bridges Click's synchronous command handlers with asyncio.
    If an event loop is already running (e.g. Jupyter), dispatches
    to a thread pool; otherwise calls asyncio.run() directly.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def get_github_token() -> str | None:
    """Return the GitHub token from environment, or None.

    Checks GITHUB_TOKEN first, then GH_TOKEN.
    """
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def create_github_client(token: str | None, timeout: float = 30.0) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient configured for the GitHub API.

    Args:
        token: GitHub personal access token. If None, no Authorization header is set.
        timeout: Request timeout in seconds (default 30.0).

    Returns:
        An httpx.AsyncClient — caller is responsible for closing it (use as async context manager).
    """
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(headers=headers, timeout=timeout)

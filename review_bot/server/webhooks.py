"""GitHub webhook endpoint with HMAC validation and event routing."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re

from fastapi import APIRouter, Header, HTTPException, Request

from review_bot.server.queue import AsyncJobQueue, ReviewJob

logger = logging.getLogger("review-bot")

router = APIRouter()

# Module-level references set during app startup
_job_queue: AsyncJobQueue | None = None
_webhook_secret: str = ""
_persona_store = None


def configure(
    job_queue: AsyncJobQueue,
    webhook_secret: str,
    persona_store,
) -> None:
    """Configure the webhook module with runtime dependencies.

    Called during app startup to inject the job queue and secret.
    """
    global _job_queue, _webhook_secret, _persona_store  # noqa: PLW0603
    _job_queue = job_queue
    _webhook_secret = webhook_secret
    _persona_store = persona_store


def _verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub HMAC-SHA256 webhook signature."""
    if not secret:
        logger.warning(
            "Webhook secret is not configured — HMAC validation is disabled. "
            "Set REVIEW_BOT_WEBHOOK_SECRET for production use."
        )
        return True  # No secret configured, skip validation
    if not signature:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _bot_username_to_persona(bot_username: str) -> str | None:
    """Map a GitHub bot username to a persona name.

    Convention: bot display name is '<persona>-bot', and the GitHub
    username for the app is typically '<persona>-bot[bot]'.
    """
    # Strip [bot] suffix added by GitHub Apps
    name = re.sub(r"\[bot\]$", "", bot_username)
    # Strip -bot suffix from our naming convention
    name = re.sub(r"-bot$", "", name)
    return name if name else None


def _parse_review_command(body: str) -> list[str]:
    """Parse /review-as commands from a comment body.

    Supports:
      /review-as deepam
      /review-as deepam,sarah
      /review-as deepam sarah
    """
    matches = re.findall(r"/review-as\s+([^\n]+)", body, re.IGNORECASE)
    personas: list[str] = []
    for match in matches:
        # Split by comma or whitespace
        names = re.split(r"[,\s]+", match.strip())
        personas.extend(n.strip() for n in names if n.strip())
    return personas


@router.post("/webhook")
async def webhook_handler(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict:
    """Handle incoming GitHub webhook events.

    Validates HMAC signature, then routes to appropriate handler
    based on the event type.
    """
    payload = await request.body()

    # HMAC-SHA256 signature validation
    if _webhook_secret and not _verify_signature(
        payload, x_hub_signature_256 or "", _webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Reject webhooks during graceful shutdown drain
    if _job_queue is not None and getattr(_job_queue, "is_draining", False) is True:
        raise HTTPException(
            status_code=503,
            detail="Server is shutting down, not accepting new webhooks",
        )

    data = await request.json()
    event = x_github_event or ""

    logger.info("Received webhook event: %s", event)

    if event == "pull_request" and data.get("action") == "review_requested":
        await _handle_review_requested(data)
    elif event == "issue_comment" and data.get("action") == "created":
        await _handle_issue_comment(data)
    elif event == "pull_request" and data.get("action") == "labeled":
        await _handle_label_event(data)

    return {"status": "ok"}


async def _handle_review_requested(data: dict) -> None:
    """Handle pull_request review_requested events.

    Maps the requested reviewer's bot username to a persona and queues
    a review job.
    """
    pr = data.get("pull_request", {})
    repo = data.get("repository", {})
    installation = data.get("installation", {})

    requested_reviewer = data.get("requested_reviewer", {})
    bot_username = requested_reviewer.get("login", "")

    persona_name = _bot_username_to_persona(bot_username)
    if not persona_name:
        logger.warning("Could not map bot username '%s' to persona", bot_username)
        return

    owner, repo_name = _extract_owner_repo(repo)
    pr_number = pr.get("number", 0)
    installation_id = installation.get("id", 0)

    if not await _persona_exists(persona_name):
        await _post_persona_not_found(
            owner, repo_name, pr_number, installation_id, persona_name
        )
        return

    await _enqueue_review(owner, repo_name, pr_number, persona_name, installation_id)


async def _handle_issue_comment(data: dict) -> None:
    """Handle issue_comment created events.

    Parses /review-as commands and queues reviews for each persona.
    """
    comment = data.get("comment", {})
    issue = data.get("issue", {})
    repo = data.get("repository", {})
    installation = data.get("installation", {})

    # Only handle comments on pull requests
    if "pull_request" not in issue:
        return

    body = comment.get("body", "")
    personas = _parse_review_command(body)
    if not personas:
        return

    owner, repo_name = _extract_owner_repo(repo)
    pr_number = issue.get("number", 0)
    installation_id = installation.get("id", 0)

    for persona_name in personas:
        if not await _persona_exists(persona_name):
            await _post_persona_not_found(
                owner, repo_name, pr_number, installation_id, persona_name
            )
            continue
        await _enqueue_review(
            owner, repo_name, pr_number, persona_name, installation_id
        )


async def _handle_label_event(data: dict) -> None:
    """Handle pull_request labeled events.

    Detects 'review:<name>' labels and queues review for the persona.
    """
    label = data.get("label", {})
    pr = data.get("pull_request", {})
    repo = data.get("repository", {})
    installation = data.get("installation", {})

    label_name = label.get("name", "")
    if not label_name.startswith("review:"):
        return

    persona_name = label_name.removeprefix("review:").strip()
    if not persona_name:
        return

    owner, repo_name = _extract_owner_repo(repo)
    pr_number = pr.get("number", 0)
    installation_id = installation.get("id", 0)

    if not await _persona_exists(persona_name):
        await _post_persona_not_found(
            owner, repo_name, pr_number, installation_id, persona_name
        )
        return

    await _enqueue_review(owner, repo_name, pr_number, persona_name, installation_id)


def _extract_owner_repo(repo_data: dict) -> tuple[str, str]:
    """Extract owner and repo name from repository webhook payload."""
    full_name = repo_data.get("full_name", "/")
    parts = full_name.split("/", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


async def _persona_exists(persona_name: str) -> bool:
    """Check if a persona exists in the store."""
    if _persona_store is None:
        return False
    return _persona_store.exists(persona_name)


async def _post_persona_not_found(
    owner: str,
    repo: str,
    pr_number: int,
    installation_id: int,
    persona_name: str,
) -> None:
    """Post a comment on the PR that the persona was not found."""
    logger.warning("Persona '%s' not found, posting comment", persona_name)
    if _job_queue is None:
        return
    try:
        from review_bot.github.api import GitHubAPIClient

        http_client = await _job_queue._github_auth.create_token_client(installation_id)
        try:
            client = GitHubAPIClient(http_client)
            await client.post_comment(
                owner,
                repo,
                pr_number,
                f"No persona configured for '{persona_name}'. "
                f"Use `review-bot persona mine` to create one.",
            )
        finally:
            await http_client.aclose()
    except Exception:
        logger.exception("Failed to post persona-not-found comment for '%s'", persona_name)


async def _enqueue_review(
    owner: str,
    repo: str,
    pr_number: int,
    persona_name: str,
    installation_id: int,
) -> None:
    """Create and enqueue a review job."""
    if _job_queue is None:
        logger.error("Job queue not initialized, cannot enqueue review")
        return

    job = ReviewJob(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        persona_name=persona_name,
        installation_id=installation_id,
    )
    await _job_queue.enqueue(job)

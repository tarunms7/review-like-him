"""GitHub webhook endpoint with HMAC validation and event routing."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

from review_bot.persona.store import PersonaStore
from review_bot.review.feedback import FeedbackStore
from review_bot.server.queue import AsyncJobQueue, ReviewJob

logger = logging.getLogger("review-bot")

router = APIRouter()

MAX_PERSONAS_PER_PR: int = 5


@dataclass
class WebhookContext:
    """Runtime dependencies for webhook handlers, injected at startup."""

    job_queue: AsyncJobQueue
    webhook_secret: str
    persona_store: PersonaStore
    feedback_store: FeedbackStore | None = None
    strict_hmac: bool = True


_context: WebhookContext | None = None


def _get_context() -> WebhookContext:
    """Return the current webhook context, or raise if not configured."""
    if _context is None:
        raise RuntimeError(
            "Webhook module not configured. Call configure() during app startup."
        )
    return _context


def configure(
    job_queue: AsyncJobQueue,
    webhook_secret: str,
    persona_store: PersonaStore,
    feedback_store: FeedbackStore | None = None,
    strict_hmac: bool = True,
) -> None:
    """Configure the webhook module with runtime dependencies.

    Called during app startup to inject the job queue, secret, and stores.

    Args:
        job_queue: The async job queue for review processing.
        webhook_secret: GitHub webhook HMAC secret.
        persona_store: PersonaStore instance for persona lookups.
        feedback_store: Optional FeedbackStore for recording feedback events.
        strict_hmac: When True (default), reject webhooks if no secret is
            configured. When False, allow unverified webhooks with a warning
            (for testing/development only).
    """
    global _context  # noqa: PLW0603
    _context = WebhookContext(
        job_queue=job_queue,
        webhook_secret=webhook_secret,
        persona_store=persona_store,
        feedback_store=feedback_store,
        strict_hmac=strict_hmac,
    )


def _verify_signature(
    payload: bytes, signature: str, secret: str, *, strict_hmac: bool = True
) -> bool:
    """Verify GitHub HMAC-SHA256 webhook signature.

    Args:
        payload: Raw request body bytes.
        signature: Value of the ``X-Hub-Signature-256`` header.
        secret: The shared HMAC secret.
        strict_hmac: When *True* (default) and *secret* is empty, verification
            fails.  When *False*, an empty secret logs a warning and allows the
            request (backward-compatible behaviour for testing).
    """
    if not secret:
        if strict_hmac:
            logger.error(
                "Webhook secret is not configured and strict_hmac is enabled — "
                "rejecting request. Set REVIEW_BOT_WEBHOOK_SECRET."
            )
            return False
        logger.warning(
            "Webhook secret is not configured — HMAC validation is disabled. "
            "Set REVIEW_BOT_WEBHOOK_SECRET for production use."
        )
        return True
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
    ctx = _get_context()
    payload = await request.body()

    # HMAC-SHA256 signature validation
    if not _verify_signature(
        payload,
        x_hub_signature_256 or "",
        ctx.webhook_secret,
        strict_hmac=ctx.strict_hmac,
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Reject webhooks during graceful shutdown drain
    if getattr(ctx.job_queue, "is_draining", False) is True:
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
    elif event == "pull_request_review_comment" and data.get("action") == "created":
        await _handle_review_comment_reply(data)
    elif event == "pull_request_review" and data.get("action") == "dismissed":
        await _handle_review_dismissed(data)

    return {"status": "ok"}


async def _deduplicated_enqueue(
    owner: str,
    repo: str,
    pr_number: int,
    persona_names: list[str],
    installation_id: int,
) -> list[str]:
    """Deduplicate and enqueue reviews for multiple personas.

    Skips duplicate persona names within a single event, enforces the
    MAX_PERSONAS_PER_PR limit, checks persona existence, and enqueues
    valid personas.

    Returns:
        List of persona names that were actually enqueued.
    """
    seen: set[str] = set()
    enqueued: list[str] = []

    for persona_name in persona_names:
        if persona_name in seen:
            logger.debug("Skipping duplicate persona '%s' in same event", persona_name)
            continue
        seen.add(persona_name)

        if len(enqueued) >= MAX_PERSONAS_PER_PR:
            logger.warning(
                "MAX_PERSONAS_PER_PR (%d) reached for %s/%s#%d, skipping '%s'",
                MAX_PERSONAS_PER_PR,
                owner,
                repo,
                pr_number,
                persona_name,
            )
            continue

        if not await _persona_exists(persona_name):
            await _post_persona_not_found(
                owner, repo, pr_number, installation_id, persona_name
            )
            continue

        await _enqueue_review(owner, repo, pr_number, persona_name, installation_id)
        enqueued.append(persona_name)

    return enqueued


async def _handle_review_requested(data: dict) -> None:
    """Handle pull_request review_requested events.

    Maps the requested reviewer's bot username to a persona and queues
    a review job. Also checks all requested_reviewers on the PR for
    multiple bot reviewers.
    """
    pr = data.get("pull_request", {})
    repo = data.get("repository", {})
    installation = data.get("installation", {})

    owner, repo_name = _extract_owner_repo(repo)
    pr_number = pr.get("number", 0)
    installation_id = installation.get("id", 0)

    persona_names: list[str] = []

    # Single requested reviewer from the event
    requested_reviewer = data.get("requested_reviewer", {})
    bot_username = requested_reviewer.get("login", "")
    persona_name = _bot_username_to_persona(bot_username)
    if persona_name:
        persona_names.append(persona_name)

    # Also check all requested_reviewers on the PR itself
    for reviewer in pr.get("requested_reviewers", []):
        login = reviewer.get("login", "")
        mapped = _bot_username_to_persona(login)
        if mapped and mapped not in persona_names:
            persona_names.append(mapped)

    if not persona_names:
        logger.warning("Could not map bot username '%s' to persona", bot_username)
        return

    await _deduplicated_enqueue(
        owner, repo_name, pr_number, persona_names, installation_id
    )


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

    await _deduplicated_enqueue(
        owner, repo_name, pr_number, personas, installation_id
    )


async def _handle_label_event(data: dict) -> None:
    """Handle pull_request labeled events.

    Checks all 'review:<name>' labels on the PR and queues reviews
    for each corresponding persona via deduplicated enqueue.
    """
    label = data.get("label", {})
    pr = data.get("pull_request", {})
    repo = data.get("repository", {})
    installation = data.get("installation", {})

    # Only trigger on review: labels
    label_name = label.get("name", "")
    if not label_name.startswith("review:"):
        return

    owner, repo_name = _extract_owner_repo(repo)
    pr_number = pr.get("number", 0)
    installation_id = installation.get("id", 0)

    # Collect all review: labels on the PR (includes the newly added one)
    persona_names: list[str] = []
    all_labels = pr.get("labels", [])

    # Ensure the triggering label is included even if not yet in pr.labels
    seen_label_names = {lbl.get("name", "") for lbl in all_labels}
    if label_name not in seen_label_names:
        all_labels = [*all_labels, label]

    for lbl in all_labels:
        name = lbl.get("name", "")
        if name.startswith("review:"):
            persona = name.removeprefix("review:").strip()
            if persona:
                persona_names.append(persona)

    if not persona_names:
        return

    await _deduplicated_enqueue(
        owner, repo_name, pr_number, persona_names, installation_id
    )


def _extract_owner_repo(repo_data: dict) -> tuple[str, str]:
    """Extract owner and repo name from repository webhook payload."""
    full_name = repo_data.get("full_name", "/")
    parts = full_name.split("/", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


async def _persona_exists(persona_name: str) -> bool:
    """Check if a persona exists in the store."""
    ctx = _get_context()
    return ctx.persona_store.exists(persona_name)


async def _post_persona_not_found(
    owner: str,
    repo: str,
    pr_number: int,
    installation_id: int,
    persona_name: str,
) -> None:
    """Post a comment on the PR that the persona was not found."""
    ctx = _get_context()
    logger.warning("Persona '%s' not found, posting comment", persona_name)
    try:
        from review_bot.github.api import GitHubAPIClient

        http_client = await ctx.job_queue._github_auth.create_token_client(installation_id)
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
    except httpx.HTTPError:
        logger.exception("Failed to post persona-not-found comment for '%s'", persona_name)


async def _enqueue_review(
    owner: str,
    repo: str,
    pr_number: int,
    persona_name: str,
    installation_id: int,
) -> None:
    """Create and enqueue a review job."""
    ctx = _get_context()
    job = ReviewJob(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        persona_name=persona_name,
        installation_id=installation_id,
    )
    await ctx.job_queue.enqueue(job)


async def _handle_review_comment_reply(data: dict) -> None:
    """Handle pull_request_review_comment created events.

    Detects replies to bot comments and records feedback based on
    simple sentiment analysis of the reply body.
    """
    ctx = _get_context()
    if ctx.feedback_store is None:
        return

    comment = data.get("comment", {})
    in_reply_to_id = comment.get("in_reply_to_id")
    if not in_reply_to_id:
        return  # Not a reply

    user = comment.get("user", {})
    username = user.get("login", "")
    body = comment.get("body", "").lower()

    # Determine if the replier is the PR author
    pr = data.get("pull_request", {})
    pr_author = pr.get("user", {}).get("login", "")
    is_pr_author = username == pr_author

    # Simple sentiment analysis
    feedback_type = _analyze_reply_sentiment(body)

    from review_bot.review.feedback import FeedbackEvent

    event = FeedbackEvent(
        comment_id=in_reply_to_id,
        feedback_type=feedback_type,
        feedback_source="reply",
        reactor_username=username,
        is_pr_author=is_pr_author,
    )
    try:
        await ctx.feedback_store.record_feedback(event)
        logger.info(
            "Recorded %s reply feedback from %s on comment %d",
            feedback_type, username, in_reply_to_id,
        )
    except (httpx.HTTPError, KeyError, ValueError):
        logger.exception("Failed to record reply feedback for comment %d", in_reply_to_id)


async def _handle_review_dismissed(data: dict) -> None:
    """Handle pull_request_review dismissed events.

    Creates negative feedback for all tracked comments in the dismissed review.
    """
    ctx = _get_context()
    if ctx.feedback_store is None:
        return

    # The person who dismissed the review
    sender = data.get("sender", {}).get("login", "")

    # Determine if sender is PR author
    pr = data.get("pull_request", {})
    pr_author = pr.get("user", {}).get("login", "")
    is_pr_author = sender == pr_author

    from review_bot.review.feedback import FeedbackEvent

    review = data.get("review", {})

    # We don't have direct access to review comments from the dismissed event,
    # but we record a feedback event for the review ID if available
    review_id = str(review.get("id", ""))
    if not review_id:
        return

    # Record dismissal as negative feedback using the review node_id
    # The actual comment mapping happens via review_comment_tracking
    event = FeedbackEvent(
        comment_id=review.get("id", 0),
        feedback_type="negative",
        feedback_source="dismissed",
        reactor_username=sender,
        is_pr_author=is_pr_author,
    )
    try:
        await ctx.feedback_store.record_feedback(event)
        logger.info(
            "Recorded dismissed review feedback from %s for review %s",
            sender, review_id,
        )
    except (httpx.HTTPError, KeyError):
        logger.exception("Failed to record dismissed review feedback for %s", review_id)


def _analyze_reply_sentiment(body: str) -> str:
    """Analyze the sentiment of a reply comment body.

    Uses simple keyword matching for classification.

    Args:
        body: Lowercased comment body text.

    Returns:
        Feedback type: 'positive', 'negative', 'confused', or 'neutral'.
    """
    positive_keywords = [
        "thanks", "thank you", "good catch", "great point", "agreed",
        "nice", "fixed", "will fix", "good find", "makes sense",
    ]
    negative_keywords = [
        "disagree", "wrong", "incorrect", "not relevant", "false positive",
        "not a bug", "intentional", "by design", "nit", "nitpick",
    ]
    confused_keywords = [
        "what do you mean", "confused", "don't understand",
        "can you explain", "unclear", "?",
    ]

    for keyword in positive_keywords:
        if keyword in body:
            return "positive"
    for keyword in negative_keywords:
        if keyword in body:
            return "negative"
    for keyword in confused_keywords:
        if keyword in body:
            return "confused"
    return "neutral"

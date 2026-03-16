"""Async job queue for review processing with SQLite status tracking."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from review_bot.github.api import GitHubAPIClient
from review_bot.github.app import GitHubAppAuth
from review_bot.notifications.base import NotificationDispatcher, NotificationMessage
from review_bot.persona.store import PersonaStore
from review_bot.review.formatter import ReviewResult
from review_bot.review.orchestrator import ReviewOrchestrator

logger = logging.getLogger("review-bot")

MULTI_REVIEW_DELAY_SECONDS: float = 2.0


class ReviewJob:
    """A queued review job for the async worker to process."""

    def __init__(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        persona_name: str,
        installation_id: int,
    ) -> None:
        self.id: str = str(uuid.uuid4())
        self.owner = owner
        self.repo = repo
        self.pr_number = pr_number
        self.persona_name = persona_name
        self.installation_id = installation_id
        self.status: str = "queued"
        self.queued_at: str = datetime.now(tz=UTC).isoformat()
        self.started_at: str | None = None
        self.completed_at: str | None = None
        self.error_message: str | None = None


class AsyncJobQueue:
    """Asyncio-based job queue with SQLite status tracking.

    Designed for later upgrade to Redis by swapping enqueue/dequeue.
    """

    def __init__(
        self,
        db_engine: AsyncEngine,
        github_auth: GitHubAppAuth,
        persona_store: PersonaStore,
    ) -> None:
        self._queue: asyncio.Queue[ReviewJob] = asyncio.Queue()
        self._db_engine = db_engine
        self._github_auth = github_auth
        self._persona_store = persona_store
        self._worker_task: asyncio.Task | None = None
        self._current_job_id: str | None = None
        self._notification_dispatcher: NotificationDispatcher | None = None

    @property
    def queue_depth(self) -> int:
        """Return the current number of queued jobs.

        Returns:
            Number of jobs waiting in the queue.
        """
        return self._queue.qsize()

    @property
    def worker_status(self) -> str:
        """Return the current worker status.

        Returns:
            'running' if worker task is active, 'stopped' if None,
            'dead' if the task has completed unexpectedly.
        """
        if self._worker_task is None:
            return "stopped"
        if self._worker_task.done():
            return "dead"
        return "running"

    @property
    def current_job_id(self) -> str | None:
        """Return the ID of the currently processing job.

        Returns:
            Job ID string or None if idle.
        """
        return self._current_job_id

    async def enqueue(self, job: ReviewJob) -> str | None:
        """Add a review job to the queue and persist status.

        Returns the job ID, or None if the job is a duplicate.
        """
        if await self._is_duplicate(job):
            logger.info(
                "Skipping duplicate job: %s/%s#%d as '%s'",
                job.owner,
                job.repo,
                job.pr_number,
                job.persona_name,
            )
            return None

        await self._persist_job(job)
        await self._queue.put(job)
        logger.info(
            "Enqueued job %s: %s/%s#%d as '%s'",
            job.id,
            job.owner,
            job.repo,
            job.pr_number,
            job.persona_name,
        )
        return job.id

    async def _is_duplicate(self, job: ReviewJob) -> bool:
        """Check if a matching job is already queued or running."""
        try:
            async with self._db_engine.begin() as conn:
                result = await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM jobs "
                        "WHERE owner = :owner AND repo = :repo "
                        "AND pr_number = :pr AND persona_name = :persona "
                        "AND status IN ('queued', 'running')"
                    ),
                    {
                        "owner": job.owner,
                        "repo": job.repo,
                        "pr": job.pr_number,
                        "persona": job.persona_name,
                    },
                )
                count = result.scalar() or 0
                return count > 0
        except SQLAlchemyError:
            logger.exception("Failed to check duplicate for job %s", job.id)
            return False

    async def start_worker(self) -> None:
        """Start the background worker loop."""
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("Job queue worker started")

    async def stop_worker(self) -> None:
        """Stop the background worker loop gracefully."""
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
            logger.info("Job queue worker stopped")

    async def _worker_loop(self) -> None:
        """Main worker loop: dequeue jobs and run reviews."""
        while True:
            try:
                job = await self._queue.get()
                await self._process_job(job)
                self._queue.task_done()
            except asyncio.CancelledError:
                logger.debug("Worker loop cancelled")
                raise
            except Exception:
                logger.exception("Unexpected error in worker loop")

    async def _process_job(self, job: ReviewJob) -> None:
        """Process a single review job."""
        self._current_job_id = job.id
        try:
            job.status = "running"
            job.started_at = datetime.now(tz=UTC).isoformat()
            await self._update_job_status(job)

            # Post progress comment on the PR
            await self._post_progress_comment(job)

            logger.info(
                "Processing job %s: %s/%s#%d as '%s'",
                job.id,
                job.owner,
                job.repo,
                job.pr_number,
                job.persona_name,
            )

            try:
                http_client = await self._github_auth.create_token_client(
                    job.installation_id,
                )
                try:
                    github_client = GitHubAPIClient(http_client)
                    orchestrator = ReviewOrchestrator(
                        github_client=github_client,
                        persona_store=self._persona_store,
                        db_engine=self._db_engine,
                    )
                    review_result = await orchestrator.run_review(
                        owner=job.owner,
                        repo=job.repo,
                        pr_number=job.pr_number,
                        persona_name=job.persona_name,
                    )
                finally:
                    await http_client.aclose()

                job.status = "completed"
                job.completed_at = datetime.now(tz=UTC).isoformat()
                logger.info("Job %s completed successfully", job.id)

                # Dispatch notifications on success
                await self._dispatch_notification(job, review_result=review_result)

            except httpx.HTTPError as exc:
                job.status = "failed"
                job.completed_at = datetime.now(tz=UTC).isoformat()
                job.error_message = str(exc)
                logger.error("Job %s failed (HTTP): %s", job.id, exc)
                error_detail = self._classify_error(exc)
                await self._post_error_comment(job, error_detail)
                await self._dispatch_notification(job, error=exc)

            except (KeyError, ValueError) as exc:
                job.status = "failed"
                job.completed_at = datetime.now(tz=UTC).isoformat()
                job.error_message = str(exc)
                logger.error("Job %s failed (parsing): %s", job.id, exc)
                error_detail = (
                    f"Configuration or parsing error — check persona name "
                    f"and PR data. ({exc})"
                )
                await self._post_error_comment(job, error_detail)
                await self._dispatch_notification(job, error=exc)

            except Exception as exc:
                job.status = "failed"
                job.completed_at = datetime.now(tz=UTC).isoformat()
                job.error_message = str(exc)
                logger.error("Job %s failed: %s", job.id, exc)
                error_detail = (
                    f"Unexpected error — the team has been notified. "
                    f"({type(exc).__name__})"
                )
                await self._post_error_comment(job, error_detail)
                await self._dispatch_notification(job, error=exc)

            await self._update_job_status(job)

            # Delay between reviews targeting the same PR to avoid rate limits
            await self._delay_if_same_pr(job)
        finally:
            self._current_job_id = None

    async def _post_progress_comment(self, job: ReviewJob) -> None:
        """Post an in-progress comment on the PR when a review starts."""
        try:
            http_client = await self._github_auth.create_token_client(
                job.installation_id,
            )
            try:
                github_client = GitHubAPIClient(http_client)
                await github_client.post_comment(
                    job.owner,
                    job.repo,
                    job.pr_number,
                    f"🔍 **Review in progress** by **{job.persona_name}**... "
                    f"Scanning repository and analyzing changes.",
                )
            finally:
                await http_client.aclose()
        except httpx.HTTPError:
            logger.warning("Failed to post progress comment for job %s", job.id)
        except (KeyError, ValueError):
            logger.warning("Failed to build progress comment for job %s", job.id)

    @staticmethod
    def _classify_error(exc: httpx.HTTPError) -> str:
        """Return a user-friendly error description based on HTTP error type."""
        exc_str = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status == 403:
                return (
                    "Rate limited — will retry automatically on next webhook event."
                )
            if status == 404:
                return (
                    "Repository or PR not found — check permissions "
                    "and that the PR is still open."
                )
            if status == 401:
                return (
                    "Authentication failed — GitHub App installation token "
                    "may have expired."
                )
            return f"GitHub API returned HTTP {status}."
        if "timeout" in exc_str.lower():
            return (
                "Request timed out — GitHub API may be experiencing issues. "
                "Will retry on next event."
            )
        return (
            f"Network error communicating with GitHub API. ({type(exc).__name__})"
        )

    async def _post_error_comment(self, job: ReviewJob, error_detail: str) -> None:
        """Post an error comment on the PR with actionable information."""
        try:
            http_client = await self._github_auth.create_token_client(
                job.installation_id,
            )
            try:
                github_client = GitHubAPIClient(http_client)
                await github_client.post_comment(
                    job.owner,
                    job.repo,
                    job.pr_number,
                    f"⚠️ Review by **{job.persona_name}** could not be completed.\n\n"
                    f"{error_detail}",
                )
            finally:
                await http_client.aclose()
        except httpx.HTTPError:
            logger.exception("Failed to post error comment for job %s", job.id)

    async def _dispatch_notification(
        self,
        job: ReviewJob,
        *,
        review_result: ReviewResult | None = None,
        error: Exception | None = None,
    ) -> None:
        """Dispatch a notification via the configured NotificationDispatcher."""
        dispatcher = self._notification_dispatcher
        if dispatcher is None:
            return

        try:
            if review_result is not None:
                message = NotificationDispatcher.build_message_from_result(
                    review_result,
                    job.owner,
                    job.repo,
                    job.pr_number,
                )
            else:
                pr_url = (
                    f"https://github.com/{job.owner}/{job.repo}"
                    f"/pull/{job.pr_number}"
                )
                message = NotificationMessage(
                    title=f"Review Failed: {job.owner}/{job.repo}#{job.pr_number}",
                    pr_url=pr_url,
                    persona_name=job.persona_name,
                    repo=f"{job.owner}/{job.repo}",
                    pr_number=job.pr_number,
                    verdict="comment",
                    summary=str(error) if error else "Unknown error",
                    comment_count=0,
                    success=False,
                    error_message=str(error) if error else "Unknown error",
                )
            await dispatcher.notify(message)
        except Exception:
            logger.exception("Failed to dispatch notification for job %s", job.id)

    async def _delay_if_same_pr(self, job: ReviewJob) -> None:
        """Sleep briefly if the next queued job targets the same PR."""
        try:
            # Peek at the next job without removing it
            next_job = self._queue._queue[0] if not self._queue.empty() else None
            if (
                next_job is not None
                and next_job.owner == job.owner
                and next_job.repo == job.repo
                and next_job.pr_number == job.pr_number
            ):
                logger.debug(
                    "Next job targets same PR %s/%s#%d, delaying %.1fs",
                    job.owner,
                    job.repo,
                    job.pr_number,
                    MULTI_REVIEW_DELAY_SECONDS,
                )
                await asyncio.sleep(MULTI_REVIEW_DELAY_SECONDS)
        except (IndexError, AttributeError):
            pass  # Non-critical, skip delay on any error

    async def _persist_job(self, job: ReviewJob) -> None:
        """Insert a new job record into the database."""
        try:
            async with self._db_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO jobs "
                        "(id, owner, repo, pr_number, persona_name, "
                        "installation_id, status, queued_at) "
                        "VALUES "
                        "(:id, :owner, :repo, :pr_number, :persona_name, "
                        ":installation_id, :status, :queued_at)"
                    ),
                    {
                        "id": job.id,
                        "owner": job.owner,
                        "repo": job.repo,
                        "pr_number": job.pr_number,
                        "persona_name": job.persona_name,
                        "installation_id": job.installation_id,
                        "status": job.status,
                        "queued_at": job.queued_at,
                    },
                )
        except SQLAlchemyError:
            logger.exception("Failed to persist job %s", job.id)

    async def _update_job_status(self, job: ReviewJob) -> None:
        """Update job status in the database."""
        try:
            async with self._db_engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE jobs SET "
                        "status = :status, "
                        "started_at = :started_at, "
                        "completed_at = :completed_at, "
                        "error_message = :error_message "
                        "WHERE id = :id"
                    ),
                    {
                        "id": job.id,
                        "status": job.status,
                        "started_at": job.started_at,
                        "completed_at": job.completed_at,
                        "error_message": job.error_message,
                    },
                )
        except SQLAlchemyError:
            logger.exception("Failed to update job %s status", job.id)

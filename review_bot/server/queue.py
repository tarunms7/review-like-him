"""Async job queue for review processing with SQLite status tracking."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from review_bot.github.api import GitHubAPIClient
from review_bot.github.app import GitHubAppAuth
from review_bot.persona.store import PersonaStore
from review_bot.review.orchestrator import ReviewOrchestrator

logger = logging.getLogger("review-bot")


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

    async def enqueue(self, job: ReviewJob) -> str:
        """Add a review job to the queue and persist status.

        Returns the job ID.
        """
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
                    await orchestrator.run_review(
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

            except Exception as exc:
                job.status = "failed"
                job.completed_at = datetime.now(tz=UTC).isoformat()
                job.error_message = str(exc)
                logger.error("Job %s failed: %s", job.id, exc)

                # Try to post error comment on the PR
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
                            f"⚠️ Review by **{job.persona_name}** could not be completed. "
                            f"The team has been notified. Please try again later.",
                        )
                    finally:
                        await http_client.aclose()
                except Exception:
                    logger.exception("Failed to post error comment for job %s", job.id)

            await self._update_job_status(job)
        finally:
            self._current_job_id = None

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
        except Exception:
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
        except Exception:
            logger.exception("Failed to update job %s status", job.id)

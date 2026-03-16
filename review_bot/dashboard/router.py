"""Dashboard router with Jinja2 template rendering."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from review_bot.dashboard import queries

logger = logging.getLogger("review-bot")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _get_engine(request: Request):
    """Extract database engine from app state."""
    return request.app.state.db_engine


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    """Dashboard overview page showing review counts, active personas, queue depth."""
    engine = _get_engine(request)
    try:
        review_counts = await queries.get_review_counts(engine)
    except (OperationalError, SQLAlchemyError):
        logger.exception("Failed to get review counts")
        review_counts = {"24h": 0, "7d": 0, "30d": 0}

    try:
        persona_stats = await queries.get_persona_stats(engine)
        active_personas = len(persona_stats)
    except (OperationalError, SQLAlchemyError):
        logger.exception("Failed to get persona stats")
        active_personas = 0

    try:
        snapshot = await queries.get_queue_snapshot(engine)
        queue_depth = len(snapshot["queued"]) + len(snapshot["running"])
    except (OperationalError, SQLAlchemyError):
        logger.exception("Failed to get queue snapshot")
        queue_depth = 0

    # Get worker status from job_queue if available
    worker_status = "unknown"
    try:
        job_queue = request.app.state.job_queue
        worker_status = job_queue.worker_status
    except AttributeError:
        pass

    return templates.TemplateResponse(
        "overview.html",
        {
            "request": request,
            "review_counts": review_counts,
            "active_personas": active_personas,
            "queue_depth": queue_depth,
            "worker_status": worker_status,
        },
    )


@router.get("/activity", response_class=HTMLResponse)
async def activity(
    request: Request,
    page: int = 1,
    per_page: int = 50,
    persona: str | None = None,
    repo: str | None = None,
):
    """Paginated activity timeline with optional persona and repo filters."""
    # Validate pagination parameters
    page = max(1, page)
    per_page = max(1, min(per_page, 200))
    engine = _get_engine(request)
    try:
        rows, total_count = await queries.get_activity_page(
            engine, page=page, per_page=per_page, persona=persona, repo=repo
        )
    except (OperationalError, SQLAlchemyError):
        logger.exception("Failed to get activity page")
        rows, total_count = [], 0

    return templates.TemplateResponse(
        "activity.html",
        {
            "request": request,
            "rows": rows,
            "total_count": total_count,
            "page": page,
            "per_page": per_page,
            "persona": persona,
            "repo": repo,
        },
    )


@router.get("/personas", response_class=HTMLResponse)
async def personas(request: Request):
    """Per-persona aggregate statistics table."""
    engine = _get_engine(request)
    try:
        persona_stats = await queries.get_persona_stats(engine)
    except (OperationalError, SQLAlchemyError):
        logger.exception("Failed to get persona stats")
        persona_stats = []

    return templates.TemplateResponse(
        "personas.html",
        {
            "request": request,
            "persona_stats": persona_stats,
        },
    )


@router.get("/queue", response_class=HTMLResponse)
async def queue(request: Request):
    """Active, queued, and failed jobs with stale job warnings."""
    engine = _get_engine(request)
    try:
        snapshot = await queries.get_queue_snapshot(engine)
    except (OperationalError, SQLAlchemyError):
        logger.exception("Failed to get queue snapshot")
        snapshot = {"queued": [], "running": [], "failed": []}

    # Detect stale jobs (running > 10 minutes)
    stale_threshold = (datetime.now(tz=UTC) - timedelta(minutes=10)).isoformat()
    has_stale = any(
        job.get("started_at") and job["started_at"] < stale_threshold
        for job in snapshot["running"]
    )

    return templates.TemplateResponse(
        "queue.html",
        {
            "request": request,
            "snapshot": snapshot,
            "has_stale": has_stale,
        },
    )


@router.get("/config", response_class=HTMLResponse)
async def config(request: Request):
    """Read-only display of settings and persona list."""
    # Get settings - safely extract non-secret fields
    settings_dict: dict = {}
    try:
        from review_bot.config.settings import Settings

        s = Settings()
        settings_dict = {
            "host": s.host,
            "port": s.port,
            "min_severity": s.min_severity,
            "db_url": re.sub(r"://[^@]+@", "://*****@", s.db_url),
        }
    except Exception:
        logger.exception("Failed to load settings for dashboard config")

    # Get persona list
    persona_list: list[dict] = []
    try:
        persona_store = request.app.state.persona_store
        all_personas = persona_store.list_all()
        persona_list = [{"name": p.name} for p in all_personas]
    except AttributeError:
        logger.exception("Failed to load persona list for dashboard config")

    return templates.TemplateResponse(
        "config.html",
        {
            "request": request,
            "settings": settings_dict,
            "personas": persona_list,
        },
    )

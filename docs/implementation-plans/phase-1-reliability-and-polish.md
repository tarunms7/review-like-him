# Phase 1 — Reliability & Polish: Implementation Plan

> Last updated: 2026-03-15

This document provides exhaustive implementation details for all 6 Phase 1 roadmap items. Each section covers file-by-file changes, function signatures, data model changes, error scenarios, rollback strategy, testing approach, and migration/deployment notes.

---

## Table of Contents

1. [Incremental Persona Updates](#1-incremental-persona-updates)
2. [Comment Deduplication in Mining](#2-comment-deduplication-in-mining)
3. [PostgreSQL Migration Path](#3-postgresql-migration-path)
4. [Health Check Endpoint](#4-health-check-endpoint)
5. [Graceful Shutdown with Job Drain](#5-graceful-shutdown-with-job-drain)
6. [Rate Limit Dashboard / Status](#6-rate-limit-dashboard--status)

---

## 1. Incremental Persona Updates

### Overview

Only mine new reviews since the last update instead of re-fetching the entire history. Track `last_mined_at` in persona YAML and pass `created:>TIMESTAMP` to the GitHub Search API query.

### Data Model Changes

#### `review_bot/persona/profile.py` — Add `last_mined_at` field

```python
class PersonaProfile(BaseModel):
    # ... existing fields ...
    last_mined_at: str | None = Field(
        default=None,
        description="ISO 8601 timestamp of the last successful mining run. "
                    "Used by incremental mining to skip already-processed reviews.",
    )
```

The field is `str | None` rather than `datetime | None` because persona YAML stores all timestamps as ISO 8601 strings (consistent with `last_updated`). `None` means "never mined" and triggers a full mine.

### File-by-File Changes

#### `review_bot/persona/miner.py`

**New parameter on `_discover_reviewed_prs`:**

```python
async def _discover_reviewed_prs(
    self,
    username: str,
    since: str | None = None,  # NEW: ISO 8601 timestamp
    progress_callback: ProgressCallback = None,
) -> dict[str, list[int]]:
```

**Search query modification:**

```python
# Current
query = f"type:pr reviewed-by:{username}"

# New — append date filter when `since` is provided
query = f"type:pr reviewed-by:{username}"
if since:
    # GitHub Search API uses ISO 8601 date or datetime
    # Normalize to UTC and format as YYYY-MM-DDTHH:MM:SS
    query += f" created:>{since}"
```

**New parameter on `mine_user_reviews`:**

```python
async def mine_user_reviews(
    self,
    username: str,
    since: str | None = None,  # NEW
    progress_callback: ProgressCallback = None,
) -> list[dict]:
```

Passes `since` through to `_discover_reviewed_prs`.

#### `review_bot/persona/analyzer.py`

**New method for merging incremental results:**

```python
class PersonaAnalyzer:
    async def analyze_incremental(
        self,
        existing_profile: PersonaProfile,
        new_weighted_reviews: list[dict],
        all_weighted_reviews: list[dict],
    ) -> PersonaProfile:
        """Re-analyze the full merged dataset and return an updated profile.

        Args:
            existing_profile: The current persona profile to preserve overrides from.
            new_weighted_reviews: Only the newly mined reviews (for logging/metrics).
            all_weighted_reviews: Full merged + deduplicated + re-weighted dataset.

        Returns:
            Updated PersonaProfile with preserved overrides and updated last_mined_at.
        """
```

This method calls the existing `analyze()` on `all_weighted_reviews` and then:
1. Preserves `overrides` from `existing_profile`
2. Sets `last_mined_at` to the current UTC timestamp
3. Updates `mined_from` to reflect total comment count

#### `review_bot/persona/store.py`

**New method for loading raw review data:**

```python
class PersonaStore:
    def _reviews_path_for(self, name: str) -> Path:
        """Return the file path for cached review data."""
        return self._dir / f"{name}_reviews.json"

    def save_reviews(self, name: str, reviews: list[dict]) -> None:
        """Cache raw review data for incremental merging."""
        path = self._reviews_path_for(name)
        path.write_text(json.dumps(reviews, indent=2), encoding="utf-8")

    def load_reviews(self, name: str) -> list[dict]:
        """Load cached review data. Returns empty list if not found."""
        path = self._reviews_path_for(name)
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))
```

#### `review_bot/cli/persona_cmd.py`

Update the `mine` command to:
1. Check for existing persona and its `last_mined_at`
2. Pass `since` to `mine_user_reviews` when doing incremental update
3. Merge new reviews with cached reviews, deduplicate by `(repo, pr_number, comment_body, created_at)` tuple
4. Re-run `apply_weights()` on the merged dataset
5. Call `analyze_incremental()` instead of `analyze()`
6. Add `--full` flag to force a complete re-mine

### Edge Cases

| Scenario | Handling |
|----------|----------|
| **`last_mined_at` is `None` or missing** | Treat as first-time mine — run full query without `created:>` filter. Set `last_mined_at` on completion. |
| **`last_mined_at` is corrupted / unparseable** | Catch `ValueError` from `datetime.fromisoformat()`, log a warning, fall back to full mine. Reset `last_mined_at` on completion. |
| **Timezone mismatches** | GitHub API returns UTC timestamps. Normalize `last_mined_at` to UTC before constructing the query. If the stored value lacks timezone info, assume UTC. Use `datetime.fromisoformat(ts).astimezone(UTC)`. |
| **Deleted reviews since last mine** | Incremental mining only adds new reviews — it cannot detect deletions. The cached review data may contain comments for deleted reviews. This is acceptable: deleted reviews still reflect the reviewer's historical style. Document this as a known limitation. A `--full` flag re-mines everything. |
| **API pagination with date filters** | GitHub Search API caps at 1000 results per query. If >1000 new reviews exist since `last_mined_at` (extremely unlikely for incremental), the miner already handles the 1000-result cap with a warning log. No additional handling needed. |
| **Duplicate reviews in merged dataset** | Deduplicate by composite key `(repo, pr_number, comment_body, created_at)`. Two comments with identical body and timestamp on the same PR are considered the same review. |
| **Re-running temporal weighting on merged dataset** | After merging, call `temporal.apply_weights()` on the entire merged list. This ensures old comments get properly down-weighted relative to new ones. The weights are recalculated from scratch — not carried over from the previous run. |
| **GitHub Search API rate limits** | The Search API has a separate rate limit (30 requests/minute for authenticated users). The existing `_request` method handles 429 responses with exponential backoff. No additional changes needed. |

### Rollback Strategy

- The `last_mined_at` field has `default=None`, so existing persona YAML files without this field load without error (Pydantic ignores missing optional fields).
- If incremental mining produces bad results, users can run `review-bot persona mine --full` to do a complete re-mine.
- The `_reviews.json` cache files are supplementary — deleting them only means the next mine will be a full mine.

### Testing Approach

**Unit tests (`tests/test_persona.py`):**

1. `test_incremental_mine_appends_date_filter` — Mock `_request`, verify `created:>` appears in the search query when `since` is provided.
2. `test_incremental_mine_no_filter_when_since_none` — Verify no date filter when `since` is `None`.
3. `test_merge_deduplicates_reviews` — Provide overlapping review lists, verify deduplication by composite key.
4. `test_corrupted_last_mined_at_falls_back` — Set `last_mined_at` to `"not-a-date"`, verify full mine occurs with warning log.
5. `test_timezone_normalization` — Provide `last_mined_at` with various timezone formats, verify UTC normalization.
6. `test_temporal_reweighting_on_merged_data` — Verify weights are recalculated after merge.

**Integration tests:**

1. End-to-end incremental mine with mocked GitHub API responses — first mine sets `last_mined_at`, second mine only fetches new reviews.
2. Verify persona YAML contains `last_mined_at` after mining.

### Migration / Deployment Notes

- No database changes required — this is purely YAML and in-memory.
- Existing persona YAML files will seamlessly gain `last_mined_at: null` on next save.
- The `_reviews.json` cache files are created alongside persona YAML files in `~/.review-bot/personas/`.

---

## 2. Comment Deduplication in Mining

### Overview

Distinguish thread replies from standalone review comments during mining. Collapse reply chains and weight original observations higher than conversational replies.

### Data Model Changes

#### Reply detection fields in mined comment dicts

Each comment dict returned by `_fetch_reviews_for_repo` gains:

```python
{
    # ... existing fields ...
    "in_reply_to_id": int | None,   # GitHub's in_reply_to_id field
    "comment_id": int,              # GitHub comment ID for thread resolution
    "is_reply": bool,               # True if this is a reply to another comment
    "thread_root_id": int | None,   # ID of the root comment in the thread
}
```

### File-by-File Changes

#### `review_bot/persona/miner.py`

**Modify comment extraction in `_fetch_reviews_for_repo`:**

```python
for comment in comments:
    user = comment.get("user", {})
    if user and user.get("login", "").lower() == username.lower():
        in_reply_to = comment.get("in_reply_to_id")
        results.append({
            "repo": repo_full_name,
            "pr_number": pr_number,
            "comment_body": comment.get("body", ""),
            "verdict": None,
            "created_at": comment.get("created_at", ""),
            "file_path": comment.get("path", ""),
            "line": comment.get("original_line") or comment.get("line"),
            "comment_id": comment.get("id"),          # NEW
            "in_reply_to_id": in_reply_to,            # NEW
        })
```

**New module: `review_bot/persona/dedup.py`**

```python
"""Thread-aware deduplication and weighting for mined review comments."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Weight multipliers for comment types
ORIGINAL_COMMENT_WEIGHT = 1.0
REPLY_WEIGHT = 0.3
SELF_REPLY_WEIGHT = 0.2
SUBSTANTIVE_REPLY_WEIGHT = 0.7

# Minimum body length to consider a reply "substantive"
SUBSTANTIVE_MIN_LENGTH = 100

# Patterns indicating non-substantive replies
_TRIVIAL_PATTERNS = [
    "fixed",
    "done",
    "good point",
    "thanks",
    "will do",
    "updated",
    "addressed",
    "agreed",
    "ack",
    "lgtm",
    "+1",
    "nit",
    "sg",
    "sgtm",
]


def resolve_threads(comments: list[dict]) -> list[dict]:
    """Resolve reply chains and annotate comments with thread metadata.

    Args:
        comments: List of comment dicts with comment_id and in_reply_to_id fields.

    Returns:
        Annotated comment list with is_reply, thread_root_id, and
        dedup_weight fields added.
    """


def _find_thread_root(
    comment_id: int,
    parent_map: dict[int, int | None],
    visited: set[int] | None = None,
) -> int:
    """Walk the reply chain to find the root comment ID.

    Handles:
    - Deleted parent comments (in_reply_to_id points to non-existent comment)
    - Circular references (defensive — shouldn't happen but prevents infinite loops)
    """


def _classify_reply(comment: dict, all_comments_by_id: dict[int, dict]) -> float:
    """Determine the weight multiplier for a reply comment.

    Rules:
    1. Self-replies (replying to own earlier comment): SELF_REPLY_WEIGHT (0.2)
    2. Trivial replies matching _TRIVIAL_PATTERNS: REPLY_WEIGHT (0.3)
    3. Substantive replies (>100 chars, contain code blocks or specific
       technical terms): SUBSTANTIVE_REPLY_WEIGHT (0.7)
    4. Default replies: REPLY_WEIGHT (0.3)
    """


def collapse_threads(
    comments: list[dict],
    username: str,
) -> list[dict]:
    """Collapse reply chains for a specific user's comments.

    For each thread:
    1. Keep all original (non-reply) comments at full weight
    2. Mark replies and assign reduced weights
    3. Self-replies get lowest weight (conversational, not review substance)
    4. Substantive replies (long, technical) get higher weight than trivial ones

    Args:
        comments: All mined comments (may include comments from other users
                  for thread resolution context).
        username: The GitHub username whose persona is being mined.

    Returns:
        Filtered list containing only the target user's comments,
        with dedup_weight field applied.
    """
```

#### `review_bot/persona/temporal.py`

**Update `apply_weights` to incorporate dedup weight:**

```python
def apply_weights(comments: list[dict]) -> list[dict]:
    """Apply temporal weights to review comments.

    If a comment has a 'dedup_weight' field (from thread deduplication),
    the final weight is: temporal_weight * dedup_weight.
    """
    weighted: list[dict] = []
    for comment in comments:
        entry = copy.deepcopy(comment)
        created_at = entry["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        temporal_w = weight_comment(created_at)
        dedup_w = entry.get("dedup_weight", 1.0)
        entry["weight"] = temporal_w * dedup_w
        weighted.append(entry)
    return weighted
```

### Edge Cases

| Scenario | Handling |
|----------|----------|
| **Self-replies** | User replies to their own earlier comment in the same thread. Weight: `0.2`. These are typically follow-ups ("actually, also check X") that add minor context but shouldn't dominate the persona. |
| **Edited comments** | GitHub API returns the latest version of comment body. No special handling needed — we always see the final edit. The `created_at` timestamp is unchanged; `updated_at` could be checked but isn't needed for dedup. |
| **Deleted parent comments** | `in_reply_to_id` points to a comment ID that doesn't exist in our fetched data. `_find_thread_root` handles this by treating the reply as a root comment (it's orphaned). Weight: `ORIGINAL_COMMENT_WEIGHT`. |
| **Nested reply chains** | A replies to B, B replies to C. `_find_thread_root` walks the chain recursively with cycle detection. All replies point to the same root. Each reply is weighted independently based on its content. |
| **Comments that are both substantive AND replies** | A reply that contains >100 chars, code blocks (`` ``` ``), or technical terms gets `SUBSTANTIVE_REPLY_WEIGHT` (0.7) instead of the default `REPLY_WEIGHT` (0.3). This preserves detailed technical responses that happen to be replies. |
| **Cross-user thread context** | Mining fetches ALL comments on a PR (not just the target user's). Non-target-user comments are used for thread resolution but filtered out before returning. This ensures `in_reply_to_id` can be resolved even when the parent comment is from another user. |
| **Comments with no `in_reply_to_id`** | Treated as original/standalone comments. Weight: `ORIGINAL_COMMENT_WEIGHT` (1.0). This is the default case for top-level review comments. |
| **Comments with `in_reply_to_id` but `comment_id` is None** | Defensive case — GitHub API should always provide `id`. If missing, treat as standalone. Log a warning. |

### Rollback Strategy

- The `dedup_weight` field is optional in temporal weighting — if absent, defaults to `1.0`. Old cached review data without this field works unchanged.
- The new `dedup.py` module is purely additive. Removing it and reverting the miner changes restores the original behavior.
- No database or YAML schema changes required.

### Testing Approach

**Unit tests (`tests/test_dedup.py` — new file):**

1. `test_standalone_comments_get_full_weight` — Comments without `in_reply_to_id` get `dedup_weight=1.0`.
2. `test_reply_gets_reduced_weight` — Comment with `in_reply_to_id` gets `dedup_weight=0.3`.
3. `test_self_reply_gets_lowest_weight` — User replying to own comment gets `dedup_weight=0.2`.
4. `test_substantive_reply_gets_higher_weight` — Long reply with code block gets `dedup_weight=0.7`.
5. `test_trivial_reply_detection` — Comments matching trivial patterns ("fixed", "done") get `REPLY_WEIGHT`.
6. `test_deleted_parent_treated_as_root` — Reply pointing to nonexistent parent treated as standalone.
7. `test_circular_reference_handling` — Defensive test for impossible but guarded-against circular reply chains.
8. `test_nested_chain_resolution` — 3-level reply chain correctly resolves root.
9. `test_cross_user_filtering` — Non-target-user comments used for resolution but excluded from output.
10. `test_temporal_weight_incorporates_dedup` — Verify `weight = temporal * dedup_weight`.

**Integration tests:**

1. Full mining pipeline with mocked GitHub API responses containing threaded comments — verify final persona weights reflect deduplication.

### Migration / Deployment Notes

- No database changes.
- Existing cached `_reviews.json` files lack `comment_id` and `in_reply_to_id`. A re-mine (`--full`) is needed to populate these fields. Incremental mining will only add them for new comments.
- The dedup module should be imported and called between mining and temporal weighting in the persona build pipeline.

---

## 3. PostgreSQL Migration Path

### Overview

Replace the async SQLite backend (`aiosqlite`) with PostgreSQL via `asyncpg` for team deployments. Support both backends via a configurable `DATABASE_URL`.

### Data Model Changes

#### `review_bot/config/settings.py`

```python
class Settings(BaseSettings):
    # Replace db_url with database_url for clarity
    database_url: str = Field(
        default=f"sqlite+aiosqlite:///{DB_PATH}",
        alias="DATABASE_URL",  # Also accept standard DATABASE_URL env var
        description="Database connection URL. Supports sqlite+aiosqlite:// and postgresql+asyncpg://",
    )

    # NEW — connection pool settings (only apply to PostgreSQL)
    db_pool_min_size: int = Field(default=2, description="Minimum connection pool size")
    db_pool_max_size: int = Field(default=10, description="Maximum connection pool size")
    db_pool_max_overflow: int = Field(
        default=5,
        description="Max connections beyond pool_max_size (temporary overflow)",
    )
    db_pool_recycle: int = Field(
        default=3600,
        description="Seconds before a connection is recycled (prevents stale connections)",
    )
```

**Backward compatibility:** The old `db_url` field is replaced with `database_url`. A `@field_validator` or `model_validator` can accept `REVIEW_BOT_DB_URL` and map it to `database_url` for backward compat.

#### Schema Compatibility: SQLite vs PostgreSQL

| SQLite Type | PostgreSQL Type | Migration Note |
|-------------|-----------------|----------------|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `SERIAL PRIMARY KEY` (or `BIGSERIAL`) | Use `GENERATED ALWAYS AS IDENTITY` for PostgreSQL 10+ |
| `TEXT` (for timestamps) | `TIMESTAMPTZ` | Store as ISO 8601 text in both for now; migrate to native timestamps later with Alembic |
| `TEXT` (for IDs) | `TEXT` or `UUID` | Keep as `TEXT` for compatibility |
| `INTEGER` | `INTEGER` (or `BIGINT` for installation_id) | `BIGINT` for installation IDs since GitHub IDs can exceed 32-bit |

### File-by-File Changes

#### `review_bot/config/settings.py`

- Add `database_url` field with alias `DATABASE_URL`
- Add pool configuration fields: `db_pool_min_size`, `db_pool_max_size`, `db_pool_max_overflow`, `db_pool_recycle`
- Add `@property` for `db_backend` that returns `"sqlite"` or `"postgresql"` based on URL prefix
- Add validation: `db_pool_min_size <= db_pool_max_size`

```python
@property
def db_backend(self) -> str:
    """Return the database backend type based on the URL."""
    if self.database_url.startswith("sqlite"):
        return "sqlite"
    if self.database_url.startswith("postgresql"):
        return "postgresql"
    raise ValueError(f"Unsupported database URL scheme: {self.database_url}")
```

#### `review_bot/server/app.py`

**Engine creation with backend-aware pool configuration:**

```python
async def _create_engine(settings: Settings) -> AsyncEngine:
    """Create a SQLAlchemy async engine with backend-appropriate settings."""
    if settings.db_backend == "postgresql":
        engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_size=settings.db_pool_max_size,
            max_overflow=settings.db_pool_max_overflow,
            pool_recycle=settings.db_pool_recycle,
            pool_pre_ping=True,  # Verify connections before use
        )
    else:
        # SQLite — no pool configuration needed (single-writer)
        engine = create_async_engine(settings.database_url, echo=False)
    return engine
```

**Backend-aware SQL for table creation:**

```python
_CREATE_TABLES_SQLITE = [...]  # Current SQL (unchanged)

_CREATE_TABLES_POSTGRESQL = [
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        persona_name TEXT NOT NULL,
        repo TEXT NOT NULL,
        pr_number INTEGER NOT NULL,
        pr_url TEXT NOT NULL,
        verdict TEXT NOT NULL,
        comment_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        duration_ms INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        repo TEXT NOT NULL,
        pr_number INTEGER NOT NULL,
        persona_name TEXT NOT NULL,
        installation_id BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        error_message TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_stats (
        persona_name TEXT PRIMARY KEY,
        total_reviews INTEGER NOT NULL DEFAULT 0,
        repos_mined INTEGER NOT NULL DEFAULT 0,
        comments_mined INTEGER NOT NULL DEFAULT 0,
        last_mined_at TIMESTAMPTZ,
        last_review_at TIMESTAMPTZ
    )
    """,
]


async def _init_database(engine: AsyncEngine, backend: str) -> None:
    """Create database tables and indexes if they don't exist."""
    tables_sql = (
        _CREATE_TABLES_POSTGRESQL if backend == "postgresql"
        else _CREATE_TABLES_SQLITE
    )
    async with engine.begin() as conn:
        for sql in tables_sql:
            await conn.execute(text(sql))
        for sql in _CREATE_INDEXES_SQL:  # Indexes are identical for both
            await conn.execute(text(sql))
    logger.info("Database tables and indexes initialized (%s)", backend)
```

#### New file: `review_bot/db/migration.py`

```python
"""Data migration utilities for SQLite → PostgreSQL."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("review-bot")


async def export_sqlite_data(engine: AsyncEngine) -> dict[str, list[dict]]:
    """Export all data from SQLite tables as a dict of table_name → rows."""


async def import_to_postgresql(
    engine: AsyncEngine,
    data: dict[str, list[dict]],
) -> dict[str, int]:
    """Import exported data into PostgreSQL tables.

    Returns dict of table_name → row count imported.
    Uses INSERT ... ON CONFLICT DO NOTHING for idempotent imports.
    """


async def migrate_sqlite_to_postgresql(
    sqlite_engine: AsyncEngine,
    pg_engine: AsyncEngine,
) -> dict[str, int]:
    """Full migration: export from SQLite, import to PostgreSQL.

    Returns dict of table_name → row count migrated.
    """
```

#### New CLI command: `review_bot/cli/db_cmd.py`

```python
"""Database management CLI commands."""

@app.command("migrate")
def migrate_db(
    source: str = typer.Option(..., help="Source DATABASE_URL (sqlite)"),
    target: str = typer.Option(..., help="Target DATABASE_URL (postgresql)"),
    dry_run: bool = typer.Option(False, help="Preview migration without writing"),
) -> None:
    """Migrate data from SQLite to PostgreSQL."""
```

### Edge Cases

| Scenario | Handling |
|----------|----------|
| **Concurrent writes (PostgreSQL)** | PostgreSQL handles concurrent writes natively with row-level locking. SQLite's write lock is the reason for this migration. No application-level locking needed. |
| **Connection failure recovery** | `pool_pre_ping=True` verifies connections before use. Stale connections are replaced. SQLAlchemy raises `OperationalError` on connection failure — the existing `try/except` blocks in `_persist_job` and `_update_job_status` log and continue gracefully. |
| **Migration data integrity** | Use `INSERT ... ON CONFLICT DO NOTHING` for idempotent imports. Running migration twice won't create duplicates. |
| **Timestamp format differences** | SQLite stores timestamps as TEXT (ISO 8601). PostgreSQL uses `TIMESTAMPTZ`. The migration script parses ISO 8601 strings and inserts as proper timestamps. Queries in the application should use parameterized values (already the case). |
| **AUTOINCREMENT → IDENTITY** | SQLite `AUTOINCREMENT` prevents ID reuse. PostgreSQL `GENERATED ALWAYS AS IDENTITY` provides equivalent behavior. During migration, preserve original IDs. |
| **Installation ID overflow** | GitHub installation IDs can exceed 32-bit `INTEGER` max (2^31 - 1 = ~2.1 billion). Use `BIGINT` in PostgreSQL. SQLite `INTEGER` is already 64-bit internally. |
| **Empty database migration** | Handle gracefully — export returns empty lists, import inserts nothing. No errors. |
| **Mid-migration failure** | Wrap the PostgreSQL import in a transaction. If any insert fails, the entire import rolls back. The source SQLite is never modified. |

### Rollback Strategy

- The `database_url` field defaults to SQLite — no change for existing users.
- Reverting to SQLite only requires changing the `DATABASE_URL` env var. The application code handles both backends.
- The migration script is one-directional (SQLite → PostgreSQL). A reverse migration would require a separate `export_postgresql_data` function (not implemented in Phase 1; low priority since PostgreSQL is the upgrade path).

### Testing Approach

**Unit tests (`tests/test_config.py`):**

1. `test_database_url_default_is_sqlite` — Verify default URL.
2. `test_database_url_from_env` — Set `DATABASE_URL`, verify it's picked up.
3. `test_db_backend_sqlite` — `db_backend` returns `"sqlite"` for sqlite URLs.
4. `test_db_backend_postgresql` — `db_backend` returns `"postgresql"` for pg URLs.
5. `test_pool_size_validation` — `db_pool_min_size > db_pool_max_size` raises error.

**Unit tests (`tests/test_db_migration.py` — new file):**

1. `test_export_sqlite_data` — Create SQLite tables with data, verify export dict.
2. `test_import_idempotent` — Import same data twice, verify no duplicates.
3. `test_timestamp_conversion` — ISO 8601 text → TIMESTAMPTZ roundtrip.

**Integration tests (require PostgreSQL — run in CI with service container):**

1. `test_full_migration_sqlite_to_pg` — End-to-end migration with sample data.
2. `test_app_startup_with_postgresql` — `create_app()` with PostgreSQL URL, verify tables created.
3. `test_concurrent_writes_postgresql` — Multiple async tasks writing to jobs table simultaneously.

**Dual-backend testing strategy:**

Use `pytest.mark.parametrize` with `["sqlite", "postgresql"]` for all database tests. Skip PostgreSQL tests when `TEST_DATABASE_URL` env var is not set (local dev without PostgreSQL).

```python
@pytest.fixture(params=["sqlite", "postgresql"])
async def db_engine(request, tmp_path):
    if request.param == "postgresql":
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            pytest.skip("TEST_DATABASE_URL not set")
        engine = create_async_engine(url)
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    # ... setup and teardown ...
```

### Migration / Deployment Notes

- **New dependency:** Add `asyncpg` to `pyproject.toml` as an optional dependency: `pip install review-like-him[postgresql]`.
- **Environment variable:** Set `DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/reviewbot`.
- **Migration steps:**
  1. Deploy PostgreSQL instance.
  2. Set `DATABASE_URL` to PostgreSQL URL.
  3. Start the application once to create tables.
  4. Run `review-bot db migrate --source sqlite+aiosqlite:///old.db --target postgresql+asyncpg://...`.
  5. Verify data with `review-bot db migrate --dry-run`.
- **Connection pool tuning guidance:**
  - Small team (1-5 users): `pool_min=2, pool_max=5, overflow=3`
  - Medium team (5-20 users): `pool_min=5, pool_max=15, overflow=5`
  - Large team (20+ users): `pool_min=10, pool_max=30, overflow=10`

---

## 4. Health Check Endpoint

### Overview

Add `GET /health` returning database connectivity, queue depth, worker status, and GitHub API rate limit remaining. Support Kubernetes liveness/readiness probe semantics.

### Data Model Changes

#### Response schema

```python
@dataclasses.dataclass
class HealthResponse:
    """Health check response schema."""

    status: str  # "healthy", "degraded", "unhealthy"
    checks: dict[str, CheckResult]
    version: str
    uptime_seconds: float


@dataclasses.dataclass
class CheckResult:
    """Individual health check result."""

    status: str  # "pass", "fail", "warn"
    detail: str
    duration_ms: float | None = None
```

#### JSON response example

```json
{
    "status": "healthy",
    "version": "0.1.0",
    "uptime_seconds": 3601.5,
    "checks": {
        "database": {
            "status": "pass",
            "detail": "Connected, query latency 2ms",
            "duration_ms": 2.1
        },
        "queue": {
            "status": "pass",
            "detail": "0 jobs queued, worker idle"
        },
        "worker": {
            "status": "pass",
            "detail": "Running"
        },
        "github_api": {
            "status": "warn",
            "detail": "Rate limit: 142/5000 remaining, resets in 1832s"
        },
        "github_app": {
            "status": "pass",
            "detail": "App ID 12345, 2 cached installation tokens"
        }
    }
}
```

### File-by-File Changes

#### New file: `review_bot/server/health.py`

```python
"""Health check endpoint with Kubernetes probe support."""

from __future__ import annotations

import dataclasses
import logging
import time

from fastapi import APIRouter, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("review-bot")

router = APIRouter(tags=["health"])

# Stored at app startup
_start_time: float = 0.0

DB_QUERY_TIMEOUT = 5.0  # seconds


def set_start_time() -> None:
    """Record the application start time. Called during lifespan startup."""
    global _start_time  # noqa: PLW0603
    _start_time = time.monotonic()


@dataclasses.dataclass
class CheckResult:
    status: str  # "pass", "fail", "warn"
    detail: str
    duration_ms: float | None = None

    def to_dict(self) -> dict:
        d = {"status": self.status, "detail": self.detail}
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        return d


async def _check_database(engine: AsyncEngine) -> CheckResult:
    """Check database connectivity with a simple query.

    Uses asyncio.wait_for with DB_QUERY_TIMEOUT to prevent
    hanging on unresponsive databases.
    """


async def _check_queue(job_queue) -> CheckResult:
    """Check queue depth and worker status."""


async def _check_github_rate_limit(github_auth) -> CheckResult:
    """Check GitHub API rate limit status.

    Reads cached rate limit data (populated by item 6).
    Does NOT make a live API call — that would consume rate limit.
    """


async def _check_github_app(github_auth) -> CheckResult:
    """Check GitHub App connection status.

    Reports App ID and number of cached installation tokens.
    """


@router.get("/health")
async def health_check(request: Request) -> dict:
    """Full health check for monitoring and Kubernetes readiness probes.

    Returns 200 if all checks pass or warn.
    Returns 503 if any critical check (database, worker) fails.
    """


@router.get("/healthz")
async def liveness_check() -> dict:
    """Minimal liveness probe for Kubernetes.

    Always returns 200 if the process is running and accepting requests.
    Does NOT check dependencies — that's what readiness is for.
    """
    return {"status": "alive"}


@router.get("/readyz")
async def readiness_check(request: Request, response: Response) -> dict:
    """Readiness probe for Kubernetes.

    Returns 200 only if database is connected and worker is running.
    Returns 503 if the app cannot serve traffic.
    """
```

#### `review_bot/server/app.py`

- Import and include health router: `from review_bot.server.health import router as health_router, set_start_time`
- Call `set_start_time()` in lifespan startup
- `app.include_router(health_router)` alongside the webhook router

#### `review_bot/server/queue.py`

**Expose queue state for health checks:**

```python
class AsyncJobQueue:
    @property
    def queue_depth(self) -> int:
        """Return the number of jobs waiting in the queue."""
        return self._queue.qsize()

    @property
    def worker_status(self) -> str:
        """Return the worker status: 'running', 'stopped', or 'dead'."""
        if self._worker_task is None:
            return "stopped"
        if self._worker_task.done():
            return "dead"
        return "running"

    @property
    def current_job_id(self) -> str | None:
        """Return the ID of the currently processing job, if any."""
        return self._current_job_id  # NEW instance variable
```

Add `self._current_job_id: str | None = None` to `__init__`, set it in `_process_job`.

### Edge Cases

| Scenario | Handling |
|----------|----------|
| **Database down but server up** | `/health` returns HTTP 503 with `status: "unhealthy"`, database check shows `status: "fail"`. `/healthz` (liveness) still returns 200 — the process is alive, Kubernetes shouldn't restart it just because the DB is down. `/readyz` (readiness) returns 503 — the app can't serve traffic. |
| **Database query timeout** | Use `asyncio.wait_for(query, timeout=DB_QUERY_TIMEOUT)`. If it times out, report `status: "fail"` with detail "Query timed out after 5s". |
| **Worker task crashed** | `worker_status` returns `"dead"` if `_worker_task.done()` is True. Health check reports this as a failure. Consider auto-restarting the worker (future improvement). |
| **No rate limit data yet** | If rate limit headers haven't been received yet (no API calls made), report `status: "pass"` with detail "No rate limit data available yet". |
| **GitHub App auth expired** | `_check_github_app` reports the number of cached tokens. If the App ID is misconfigured, the check can't verify without making an API call. Report the configured App ID and let the user verify. |
| **Concurrent health check requests** | All checks are read-only — no concurrency issues. Database check uses a separate connection from the pool. |

### Rollback Strategy

- The health endpoints are purely additive. Removing the health router from `app.py` reverts to the original behavior.
- No database changes required.

### Testing Approach

**Unit tests (`tests/test_health.py` — new file):**

1. `test_healthz_always_200` — Liveness probe returns 200.
2. `test_health_all_passing` — Mock all checks passing, verify 200 and response schema.
3. `test_health_db_down_returns_503` — Mock database check failure, verify 503.
4. `test_health_db_timeout` — Mock slow database query, verify timeout handling.
5. `test_readyz_db_down_returns_503` — Readiness probe with DB failure.
6. `test_readyz_worker_dead_returns_503` — Readiness probe with dead worker.
7. `test_health_response_schema` — Validate response matches `HealthResponse` structure.
8. `test_queue_depth_reporting` — Enqueue jobs, verify health reports correct depth.
9. `test_uptime_calculation` — Verify uptime_seconds increases over time.

**Integration tests:**

1. `test_health_endpoint_with_real_db` — Start app with SQLite, hit `/health`, verify database check passes.
2. `test_health_endpoint_startup_sequence` — Hit `/health` immediately after startup, verify all checks respond.

### Migration / Deployment Notes

- **Kubernetes probes configuration:**
  ```yaml
  livenessProbe:
    httpGet:
      path: /healthz
      port: 8000
    initialDelaySeconds: 5
    periodSeconds: 10
  readinessProbe:
    httpGet:
      path: /readyz
      port: 8000
    initialDelaySeconds: 10
    periodSeconds: 5
    failureThreshold: 3
  ```
- No database migrations needed.
- The `/health` endpoint is unauthenticated — consider adding IP allowlisting or a simple bearer token for production if the endpoint is public.

---

## 5. Graceful Shutdown with Job Drain

### Overview

Handle `SIGTERM`/`SIGINT` by stopping the queue worker from accepting new jobs, waiting for in-flight reviews to complete (with a configurable timeout), then disposing database connections in the correct order.

### Data Model Changes

#### `review_bot/config/settings.py`

```python
class Settings(BaseSettings):
    # ... existing fields ...
    shutdown_drain_timeout: int = Field(
        default=30,
        description="Seconds to wait for in-flight jobs to complete during shutdown",
    )
```

### File-by-File Changes

#### `review_bot/server/queue.py`

**Replace hard cancel with drain-based shutdown:**

```python
class AsyncJobQueue:
    def __init__(self, ...) -> None:
        # ... existing init ...
        self._draining: bool = False
        self._current_job: ReviewJob | None = None
        self._current_job_id: str | None = None
        self._job_complete_event: asyncio.Event = asyncio.Event()

    async def enqueue(self, job: ReviewJob) -> str:
        """Add a review job to the queue.

        Raises:
            RuntimeError: If the queue is draining (shutdown in progress).
        """
        if self._draining:
            raise RuntimeError("Queue is draining, not accepting new jobs")
        # ... existing enqueue logic ...

    @property
    def is_draining(self) -> bool:
        """Whether the queue is in drain mode (shutdown in progress)."""
        return self._draining

    async def drain(self, timeout: float = 30.0) -> bool:
        """Initiate graceful drain: stop accepting new jobs, wait for
        in-flight work to complete.

        Args:
            timeout: Maximum seconds to wait for the current job to finish.

        Returns:
            True if drained cleanly, False if timed out (force-kill needed).
        """
        self._draining = True
        logger.info("Drain initiated, stopping new job acceptance")

        if self._current_job is None:
            logger.info("No in-flight jobs, drain complete")
            await self._cancel_worker()
            return True

        logger.info(
            "Waiting up to %.0fs for in-flight job %s to complete",
            timeout,
            self._current_job_id,
        )

        try:
            await asyncio.wait_for(
                self._job_complete_event.wait(),
                timeout=timeout,
            )
            logger.info("In-flight job completed, drain successful")
            await self._cancel_worker()
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "Drain timeout (%.0fs) exceeded, force-cancelling worker",
                timeout,
            )
            await self._cancel_worker()
            # Mark the timed-out job as failed
            if self._current_job is not None:
                self._current_job.status = "failed"
                self._current_job.error_message = "Shutdown timeout exceeded"
                self._current_job.completed_at = datetime.now(tz=UTC).isoformat()
                await self._update_job_status(self._current_job)
            return False

    async def _cancel_worker(self) -> None:
        """Cancel the worker task."""
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def _process_job(self, job: ReviewJob) -> None:
        """Process a single review job with tracking."""
        self._current_job = job
        self._current_job_id = job.id
        self._job_complete_event.clear()

        try:
            # ... existing processing logic ...
            pass
        finally:
            self._current_job = None
            self._current_job_id = None
            self._job_complete_event.set()

    async def stop_worker(self) -> None:
        """Stop the background worker loop.

        DEPRECATED: Use drain() for graceful shutdown.
        Kept for backward compatibility — calls drain with 0 timeout.
        """
        await self.drain(timeout=0)
```

#### `review_bot/server/app.py`

**Signal-aware lifespan with drain period:**

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage application startup and shutdown with graceful drain."""
    logger.info("Starting review-bot server")

    # ... existing startup code ...

    # Register signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler(sig: int) -> None:
        sig_name = signal.Signals(sig).name
        logger.info("Received %s, initiating graceful shutdown", sig_name)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler, sig)

    yield

    # Shutdown sequence
    logger.info("Shutting down review-bot server")

    # 1. Drain the job queue (wait for in-flight work)
    drained = await job_queue.drain(timeout=app_settings.shutdown_drain_timeout)
    if not drained:
        logger.warning("Shutdown drain timed out, some jobs may be incomplete")

    # 2. Dispose database connections AFTER queue drain
    #    (jobs may need DB access during drain)
    await engine.dispose()
    logger.info("Database connections disposed")
```

#### `review_bot/server/webhooks.py`

**Reject new webhooks during drain:**

```python
@router.post("/webhook")
async def webhook_handler(...) -> dict:
    # ... existing validation ...

    # Reject during drain
    if _job_queue is not None and _job_queue.is_draining:
        raise HTTPException(
            status_code=503,
            detail="Server is shutting down, not accepting new webhooks",
        )

    # ... existing routing ...
```

### Edge Cases

| Scenario | Handling |
|----------|----------|
| **Signal during startup** | If `SIGTERM` arrives before `yield` in lifespan, FastAPI handles it by not entering the yield block. The signal handler is only registered after startup completes. No partial state to clean up. |
| **Double signal (SIGTERM then SIGINT)** | The first signal sets `shutdown_event` and begins drain. A second signal during drain should force immediate exit. Implement by checking `_draining` in the signal handler — if already draining, call `sys.exit(1)`. |
| **Worker stuck in LLM call >30s** | The LLM call (Claude SDK `query()`) is an async generator. `asyncio.wait_for` wrapping the drain will trigger `TimeoutError` after 30s. The worker task is then cancelled, which raises `CancelledError` in the LLM call. The Claude SDK should handle this gracefully (close the HTTP connection). |
| **Partial comment scenarios** | A review mid-post (e.g., `post_review` called, GitHub received 3 of 5 inline comments) — GitHub's review API is atomic: either the entire review is posted or none of it. The `post_review` endpoint creates the review in one API call. No partial state. |
| **Database connection disposal order** | Dispose connections AFTER drain completes. If a job needs to update its status during drain, it needs DB access. The sequence is: drain queue → dispose DB. |
| **Queue has pending (not in-flight) jobs** | During drain, pending jobs in the asyncio.Queue are NOT processed. They remain in the queue when the worker is cancelled. Their status in the DB remains `"queued"`. On next startup, these jobs are effectively lost (they were in-memory). This is acceptable for Phase 1 — Phase 2 could add persistent queue recovery. |
| **Force-kill after timeout** | The drain method cancels the worker task on timeout. The cancelled job is marked as `"failed"` with `error_message="Shutdown timeout exceeded"`. An error comment is NOT posted to the PR (we're shutting down, don't make more API calls). |

### Rollback Strategy

- The `shutdown_drain_timeout` setting defaults to 30s. Setting it to 0 reverts to the current immediate-cancel behavior.
- The `drain()` method falls back to `_cancel_worker()` if no job is in-flight, matching the current `stop_worker()` behavior.
- Signal handler registration is additive — removing it just means the default SIGTERM/SIGINT handling (immediate process exit) applies.

### Testing Approach

**Unit tests (`tests/test_queue.py` — new file or extend `tests/test_webhooks.py`):**

1. `test_drain_no_inflight_returns_immediately` — Empty queue, drain returns True instantly.
2. `test_drain_waits_for_inflight_job` — Start a mock job, drain waits for completion.
3. `test_drain_timeout_force_cancels` — Mock a slow job, verify drain returns False after timeout.
4. `test_enqueue_during_drain_raises` — Enqueue after drain starts, verify RuntimeError.
5. `test_webhook_rejects_during_drain` — POST to /webhook during drain, verify 503.
6. `test_drain_marks_timed_out_job_failed` — Verify job status is "failed" with timeout message.
7. `test_double_signal_forces_exit` — Simulate two signals, verify immediate exit on second.
8. `test_db_dispose_after_drain` — Verify database is disposed after drain completes, not before.

**Integration tests:**

1. `test_graceful_shutdown_with_inflight_review` — Start a real review job (mocked LLM), send SIGTERM, verify it completes before shutdown.
2. `test_shutdown_timeout_with_slow_review` — Mock a 60s LLM call, 5s timeout, verify force-cancel.

### Migration / Deployment Notes

- **Kubernetes:** Set `terminationGracePeriodSeconds` to at least `shutdown_drain_timeout + 10` (e.g., 40s for default 30s drain).
- **Docker:** `docker stop` sends SIGTERM with a default 10s timeout. Set `--stop-timeout=40` to match the drain period.
- **systemd:** Set `TimeoutStopSec=40` in the service unit file.
- No database changes required.

---

## 6. Rate Limit Dashboard / Status

### Overview

Track and expose GitHub API rate limit consumption. Parse `X-RateLimit-*` headers from every GitHub API response, store the state, and expose it via an endpoint and CLI command.

### Data Model Changes

#### Rate limit state dataclass

```python
@dataclasses.dataclass
class RateLimitState:
    """Parsed rate limit state from GitHub API response headers."""

    resource: str          # "core", "search", "graphql"
    limit: int             # Total allowed requests
    remaining: int         # Remaining requests
    reset_at: int          # Unix timestamp when limit resets
    used: int              # Requests used in current window
    last_updated: float    # time.monotonic() when this was last updated

    @property
    def reset_in_seconds(self) -> int:
        """Seconds until the rate limit resets."""
        return max(0, self.reset_at - int(time.time()))

    @property
    def usage_pct(self) -> float:
        """Percentage of rate limit consumed."""
        if self.limit == 0:
            return 0.0
        return ((self.limit - self.remaining) / self.limit) * 100
```

#### Response schema for `GET /status/rate-limits`

```json
{
    "rate_limits": {
        "core": {
            "limit": 5000,
            "remaining": 4200,
            "reset_at": "2026-03-15T14:30:00Z",
            "reset_in_seconds": 1832,
            "used": 800,
            "usage_pct": 16.0
        },
        "search": {
            "limit": 30,
            "remaining": 28,
            "reset_at": "2026-03-15T14:01:00Z",
            "reset_in_seconds": 42,
            "used": 2,
            "usage_pct": 6.7
        }
    },
    "warnings": [],
    "last_updated": "2026-03-15T14:00:18Z"
}
```

### File-by-File Changes

#### `review_bot/github/api.py`

**Add rate limit tracking to `GitHubAPIClient`:**

```python
class GitHubAPIClient:
    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._client = http_client
        self._rate_limits: dict[str, RateLimitState] = {}
        self._rate_limit_lock: asyncio.Lock = asyncio.Lock()

    def _parse_rate_limit_headers(
        self,
        response: httpx.Response,
        resource: str = "core",
    ) -> None:
        """Parse X-RateLimit-* headers and update stored state.

        Headers parsed:
        - X-RateLimit-Limit
        - X-RateLimit-Remaining
        - X-RateLimit-Reset
        - X-RateLimit-Used
        - X-RateLimit-Resource (overrides the `resource` parameter if present)
        """

    async def _request(self, method, url, **kwargs) -> httpx.Response:
        """Make a request and parse rate limit headers from the response."""
        # ... existing retry logic ...
        # After getting a successful response:
        resource = self._infer_resource(url)
        self._parse_rate_limit_headers(resp, resource)
        return resp

    def _infer_resource(self, url: str) -> str:
        """Infer the rate limit resource from the URL.

        /search/* → "search"
        /graphql → "graphql"
        everything else → "core"
        """

    @property
    def rate_limits(self) -> dict[str, RateLimitState]:
        """Return a snapshot of current rate limit state."""
        return dict(self._rate_limits)
```

#### `review_bot/persona/miner.py`

**Add rate limit tracking to the miner's `_request`:**

```python
class GitHubReviewMiner:
    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._client = http_client
        self._etags: dict[str, str] = {}
        self._rate_limits: dict[str, RateLimitState] = {}  # NEW

    async def _request(self, url: str, params=None) -> httpx.Response:
        # ... existing logic ...
        # After response:
        self._parse_rate_limit_headers(response)
        return response
```

Alternatively, the miner could accept a shared `RateLimitTracker` instance to avoid duplicating parsing logic.

**Extract shared tracker: `review_bot/github/rate_limits.py`**

```python
"""GitHub API rate limit tracking and storage."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from datetime import UTC, datetime

import httpx

logger = logging.getLogger("review-bot")


@dataclasses.dataclass
class RateLimitState:
    """Parsed rate limit state from GitHub API response headers."""
    resource: str
    limit: int
    remaining: int
    reset_at: int
    used: int
    last_updated: float

    @property
    def reset_in_seconds(self) -> int:
        return max(0, self.reset_at - int(time.time()))

    @property
    def usage_pct(self) -> float:
        if self.limit == 0:
            return 0.0
        return ((self.limit - self.remaining) / self.limit) * 100

    def to_dict(self) -> dict:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_at": datetime.fromtimestamp(self.reset_at, tz=UTC).isoformat(),
            "reset_in_seconds": self.reset_in_seconds,
            "used": self.used,
            "usage_pct": round(self.usage_pct, 1),
        }


class RateLimitTracker:
    """Thread-safe tracker for GitHub API rate limit state.

    Shared across GitHubAPIClient and GitHubReviewMiner instances.
    Stored on app.state for access by health checks and status endpoints.
    """

    def __init__(self) -> None:
        self._limits: dict[str, RateLimitState] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._last_updated: float = 0.0

    async def update_from_response(
        self,
        response: httpx.Response,
        resource: str = "core",
    ) -> None:
        """Parse rate limit headers and update state.

        Thread-safe via asyncio.Lock to handle concurrent requests
        updating the same resource.
        """
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is None:
            return  # Not all responses include rate limit headers

        async with self._lock:
            actual_resource = response.headers.get("X-RateLimit-Resource", resource)
            self._limits[actual_resource] = RateLimitState(
                resource=actual_resource,
                limit=int(response.headers.get("X-RateLimit-Limit", 0)),
                remaining=int(remaining),
                reset_at=int(response.headers.get("X-RateLimit-Reset", 0)),
                used=int(response.headers.get("X-RateLimit-Used", 0)),
                last_updated=time.monotonic(),
            )
            self._last_updated = time.monotonic()

    def snapshot(self) -> dict[str, RateLimitState]:
        """Return a copy of the current rate limit state."""
        return dict(self._limits)

    @property
    def last_updated_iso(self) -> str | None:
        """ISO 8601 timestamp of the last update, or None if never updated."""
        if self._last_updated == 0.0:
            return None
        # Convert monotonic to wall clock (approximate)
        wall_time = time.time() - (time.monotonic() - self._last_updated)
        return datetime.fromtimestamp(wall_time, tz=UTC).isoformat()

    def warnings(self) -> list[str]:
        """Return warnings for any rate limits approaching exhaustion."""
        warns: list[str] = []
        for state in self._limits.values():
            if state.remaining <= 10 and state.limit > 0:
                warns.append(
                    f"{state.resource}: only {state.remaining}/{state.limit} "
                    f"remaining, resets in {state.reset_in_seconds}s"
                )
        return warns

    @staticmethod
    def infer_resource(url: str) -> str:
        """Infer the rate limit resource from the request URL."""
        if "/search/" in url:
            return "search"
        if "/graphql" in url:
            return "graphql"
        return "core"
```

#### `review_bot/server/app.py`

**Initialize and store the tracker on app state:**

```python
from review_bot.github.rate_limits import RateLimitTracker

# In lifespan:
rate_limit_tracker = RateLimitTracker()
app.state.rate_limit_tracker = rate_limit_tracker

# Pass to GitHubAPIClient and miner instances...
```

#### New endpoint in `review_bot/server/health.py` (or separate `review_bot/server/status.py`)

```python
@router.get("/status/rate-limits")
async def rate_limits_status(request: Request) -> dict:
    """Return current GitHub API rate limit state.

    Returns cached rate limit data from API response headers.
    Does NOT make a live GitHub API call.
    """
    tracker: RateLimitTracker = request.app.state.rate_limit_tracker
    snapshot = tracker.snapshot()
    return {
        "rate_limits": {
            name: state.to_dict() for name, state in snapshot.items()
        },
        "warnings": tracker.warnings(),
        "last_updated": tracker.last_updated_iso,
    }
```

#### `review_bot/cli/main.py`

**Add `status` command group:**

```python
@app.command("status")
def status_cmd(
    server_url: str = typer.Option("http://localhost:8000", help="Server URL"),
) -> None:
    """Show review-bot server status including rate limits."""
```

#### `review_bot/cli/server_cmd.py` (or new `review_bot/cli/status_cmd.py`)

```python
"""CLI command for checking server status."""

import httpx
import typer


def show_rate_limits(server_url: str = "http://localhost:8000") -> None:
    """Fetch and display rate limit status from the server.

    GET /status/rate-limits and format output as a table.
    """
    with httpx.Client() as client:
        resp = client.get(f"{server_url}/status/rate-limits")
        resp.raise_for_status()
        data = resp.json()

    # Pretty-print rate limit table
    for resource, limits in data["rate_limits"].items():
        remaining = limits["remaining"]
        total = limits["limit"]
        pct = limits["usage_pct"]
        reset = limits["reset_in_seconds"]
        bar = _progress_bar(pct)
        typer.echo(f"  {resource:10s} {bar} {remaining}/{total} remaining (resets in {reset}s)")

    if data["warnings"]:
        typer.echo("\n⚠️  Warnings:")
        for w in data["warnings"]:
            typer.echo(f"    {w}")
```

### Edge Cases

| Scenario | Handling |
|----------|----------|
| **Multiple GitHub API endpoints with different limits** | GitHub has separate rate limits for `core` (5000/hr), `search` (30/min), and `graphql` (5000/hr). The `X-RateLimit-Resource` header distinguishes them. The tracker stores per-resource state. If the header is missing, `infer_resource()` uses URL pattern matching. |
| **Installation token vs user token limits** | Installation tokens have their own rate limits (separate from user tokens). Since the app uses installation tokens exclusively (via `GitHubAppAuth`), all tracked limits are for the installation. If multiple installations are active, they share the same limit pool per GitHub App. The tracker doesn't distinguish — it reflects the most recent response from any installation. |
| **Race conditions in concurrent requests** | The `asyncio.Lock` in `RateLimitTracker.update_from_response` prevents concurrent updates from corrupting state. Lock contention is minimal — the critical section is just dict assignment. |
| **Rate limit data stale/missing** | If no API calls have been made, `snapshot()` returns an empty dict. The endpoint returns `{"rate_limits": {}, "warnings": [], "last_updated": null}`. The CLI shows "No rate limit data available". |
| **Header values are non-integer** | `int()` conversion is wrapped in try/except within `update_from_response`. Malformed headers are logged and skipped. |
| **Reset time in the past** | `reset_in_seconds` uses `max(0, ...)` — never returns negative values. A reset time in the past means the limit has already refreshed. |

### Rollback Strategy

- The `RateLimitTracker` is optional — if removed, the API client works exactly as before (headers are just ignored).
- The `/status/rate-limits` endpoint is purely additive.
- The CLI `status` command is a new subcommand — removing it doesn't affect existing commands.

### Testing Approach

**Unit tests (`tests/test_rate_limits.py` — new file):**

1. `test_parse_rate_limit_headers` — Mock response with X-RateLimit-* headers, verify state.
2. `test_infer_resource_search` — URL containing `/search/` maps to "search".
3. `test_infer_resource_core` — Default URL maps to "core".
4. `test_concurrent_updates` — Multiple async updates don't corrupt state.
5. `test_warnings_near_exhaustion` — Remaining < 10 triggers warning.
6. `test_no_warnings_when_healthy` — Remaining > 10, no warnings.
7. `test_snapshot_returns_copy` — Modifying snapshot doesn't affect tracker.
8. `test_reset_in_seconds_never_negative` — Past reset time returns 0.
9. `test_usage_pct_calculation` — Verify percentage math.
10. `test_empty_snapshot_before_any_calls` — No data returns empty dict.

**Unit tests (`tests/test_github_api.py` — extend existing):**

1. `test_request_updates_rate_limit_tracker` — Mock response, verify tracker updated.
2. `test_rate_limit_resource_header_override` — `X-RateLimit-Resource` header overrides URL inference.

**Integration tests:**

1. `test_status_endpoint_returns_rate_limits` — Start app, make a mock API call, hit `/status/rate-limits`.
2. `test_cli_status_command` — Mock server response, verify CLI output formatting.

### Migration / Deployment Notes

- No database changes required.
- The rate limit tracker is in-memory only — state is lost on restart. This is acceptable because rate limits reset hourly and the tracker repopulates from the first API response.
- The `GET /status/rate-limits` endpoint is unauthenticated (same as `/health`). Consider the same access control strategy.
- New dependency: none (uses existing `httpx`, `dataclasses`, `asyncio`).

---

## Cross-Cutting Concerns

### Dependency Summary

| Item | New Dependencies | New Files | Modified Files |
|------|-----------------|-----------|----------------|
| 1. Incremental mining | — | — | `miner.py`, `profile.py`, `store.py`, `analyzer.py`, `persona_cmd.py` |
| 2. Comment dedup | — | `persona/dedup.py` | `miner.py`, `temporal.py` |
| 3. PostgreSQL | `asyncpg` (optional) | `db/migration.py`, `cli/db_cmd.py` | `settings.py`, `app.py` |
| 4. Health check | — | `server/health.py` | `app.py`, `queue.py` |
| 5. Graceful shutdown | — | — | `app.py`, `queue.py`, `settings.py`, `webhooks.py` |
| 6. Rate limit dashboard | — | `github/rate_limits.py`, `cli/status_cmd.py` | `api.py`, `miner.py`, `app.py` |

### Recommended Implementation Order

1. **Health check endpoint** (🟢 smallest, no dependencies, immediate observability value)
2. **Rate limit dashboard** (builds on health check patterns, adds to status endpoints)
3. **Graceful shutdown** (critical for reliability, depends on queue changes from health check)
4. **Comment deduplication** (independent of infrastructure changes, improves mining quality)
5. **Incremental persona updates** (depends on dedup for proper merging)
6. **PostgreSQL migration** (🔴 largest, benefits from all other changes being stable first)

### Risk Assessment

| Risk | Mitigation |
|------|------------|
| PostgreSQL migration data loss | Idempotent import, source DB never modified, dry-run mode |
| Graceful shutdown race conditions | asyncio.Lock for shared state, event-based coordination |
| Rate limit tracker memory leak | Fixed-size dict (3 resources max), no unbounded growth |
| Incremental mining produces different persona | Full re-mine available via `--full` flag |
| Thread dedup over-filters substantive replies | Configurable weight thresholds, conservative defaults |

from __future__ import annotations

import logging

from psycopg_pool import ConnectionPool

from .config import Settings

logger = logging.getLogger("papela.db")

# Single schema bootstrap. Idempotent; safe to run on every startup.
# In a larger project this moves to Alembic/atlas; for a single-table
# pipeline an idempotent DDL block is the honest minimal choice.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id           UUID PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','processing','done','failed')),
    filename     TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    size_bytes   BIGINT NOT NULL,
    pages        INT,
    result       JSONB,
    error        TEXT,
    attempts     INT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Partial index: the worker only ever scans pending rows.
CREATE INDEX IF NOT EXISTS idx_jobs_pending
    ON jobs (created_at) WHERE status = 'pending';
"""


class Database:
    def __init__(self, settings: Settings) -> None:
        self._pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            open=False,
            kwargs={"autocommit": True},
        )

    def open(self) -> None:
        self._pool.open()
        self._pool.wait(timeout=10.0)
        with self._pool.connection() as conn:
            conn.execute(SCHEMA_SQL)
        logger.info("db.ready", extra={"request_id": None})

    def close(self) -> None:
        self._pool.close()

    @property
    def pool(self) -> ConnectionPool:
        return self._pool

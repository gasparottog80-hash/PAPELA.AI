from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

logger = logging.getLogger("papela.repo")


class JobRepository:
    """All job persistence + the Postgres-backed work queue.

    The queue is `SELECT ... FOR UPDATE SKIP LOCKED`: multiple workers can
    poll concurrently and each row is claimed by exactly one worker, with no
    external broker. On-prem, LGPD-friendly (data never leaves Postgres).
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def create(
        self, *, filename: str, storage_path: str, size_bytes: int, pages: int
    ) -> str:
        job_id = str(uuid.uuid4())
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, filename, storage_path, size_bytes, pages)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (job_id, filename, storage_path, size_bytes, pages),
            )
        return job_id

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                SELECT id::text, status, filename, pages, result, error,
                       attempts, created_at, updated_at
                FROM jobs WHERE id = %s
                """,
                (job_id,),
            )
            return cur.fetchone()

    def claim_next(self) -> Optional[dict[str, Any]]:
        """Atomically claim one pending job. Returns None if queue empty."""
        with self._pool.connection() as conn:
            with conn.transaction():
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    """
                    SELECT id, storage_path, filename, attempts
                    FROM jobs
                    WHERE status = 'pending'
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                if row is None:
                    return None
                cur.execute(
                    """
                    UPDATE jobs
                    SET status = 'processing',
                        attempts = attempts + 1,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (row["id"],),
                )
                return row

    def mark_done(self, job_id: str, result: dict[str, Any]) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'done', result = %s, error = NULL, updated_at = now()
                WHERE id = %s
                """,
                (Jsonb(result), job_id),
            )

    def mark_failed(self, job_id: str, error: str, *, max_attempts: int) -> None:
        """Failed -> back to pending for retry until attempts exhausted."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END,
                    error = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (max_attempts, error[:2000], job_id),
            )

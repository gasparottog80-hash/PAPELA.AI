from __future__ import annotations

import logging
from typing import Any

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
        self,
        *,
        job_id: str,
        filename: str,
        storage_path: str,
        size_bytes: int,
        pages: int,
    ) -> str:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, filename, storage_path, size_bytes, pages)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (job_id, filename, storage_path, size_bytes, pages),
            )
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                SELECT id::text, status, filename, pages, result, error,
                       attempts, created_at, updated_at, purged_at
                FROM jobs WHERE id = %s
                """,
                (job_id,),
            )
            return cur.fetchone()

    def claim_next(self) -> dict[str, Any] | None:
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

    def mark_done(
        self, job_id: str, result: dict[str, Any], *, purged: bool = False
    ) -> None:
        """Mark a job done. If `purged`, also stamp purged_at now (the raw PDF
        was deleted in the same step for data minimization)."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'done',
                    result = %s,
                    error = NULL,
                    purged_at = CASE WHEN %s THEN now() ELSE purged_at END,
                    updated_at = now()
                WHERE id = %s
                """,
                (Jsonb(result), purged, job_id),
            )

    def expired_done_jobs(self, *, retention_days: int) -> list[str]:
        """job_ids of done jobs older than retention whose raw PDF is not yet
        purged. Drives the periodic retention sweep."""
        with self._pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id::text
                FROM jobs
                WHERE status = 'done'
                  AND purged_at IS NULL
                  AND created_at < now() - make_interval(days => %s)
                """,
                (retention_days,),
            )
            return [row[0] for row in cur.fetchall()]

    def mark_purged(self, job_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE jobs SET purged_at = now(), updated_at = now() WHERE id = %s",
                (job_id,),
            )

    def reap_stalled_jobs(self, *, stall_timeout_s: int, max_attempts: int) -> int:
        """Recover jobs stuck in 'processing' (worker crashed between claim and
        mark_done/failed). Jobs whose updated_at is older than the timeout go
        back to 'pending' for retry, or to 'failed' once attempts are exhausted.

        Returns the number of jobs reaped. Runs in a single UPDATE so it is
        safe to call from multiple workers concurrently.
        """
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END,
                    error = CASE WHEN attempts >= %s
                                 THEN 'processing_error: StalledJobReaped'
                                 ELSE error END,
                    updated_at = now()
                WHERE status = 'processing'
                  AND updated_at < now() - make_interval(secs => %s)
                """,
                (max_attempts, max_attempts, stall_timeout_s),
            )
            return cur.rowcount

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

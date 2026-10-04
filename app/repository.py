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
        tenant_id: str,
        filename: str,
        storage_path: str,
        size_bytes: int,
        pages: int,
    ) -> str:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs
                    (id, tenant_id, filename, storage_path, size_bytes, pages)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (job_id, tenant_id, filename, storage_path, size_bytes, pages),
            )
        return job_id

    def get_for_tenant(self, job_id: str, tenant_id: str) -> dict[str, Any] | None:
        """The only job lookup exposed to HTTP handlers (404 across tenants)."""
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                SELECT id::text, status, filename, pages, result, error,
                       attempts, created_at, updated_at, purged_at
                FROM jobs WHERE id = %s AND tenant_id = %s
                """,
                (job_id, tenant_id),
            )
            return cur.fetchone()

    def get_internal(self, job_id: str) -> dict[str, Any] | None:
        """Worker-only lookup after claiming the server-generated job UUID."""
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

    def delete_terminal_for_tenant(self, job_id: str, tenant_id: str) -> bool:
        """Delete only a terminal job owned by this tenant after PDF removal."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                DELETE FROM jobs
                WHERE id = %s AND tenant_id = %s AND status IN ('done', 'failed')
                """,
                (job_id, tenant_id),
            )
            return cur.rowcount == 1

    def privacy_generation(self) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT generation, restore_ready, "
                "database_name = current_database() FROM privacy_state "
                "WHERE singleton = TRUE"
            ).fetchone()
            if row is None or not row[1] or not row[2]:
                raise RuntimeError("privacy reconciliation required")
            return int(row[0])

    def database_name(self) -> str:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT current_database()").fetchone()
            if row is None:
                raise RuntimeError("database identity unavailable")
            return str(row[0])

    def privacy_generation_raw(self) -> int:
        """Read the saved generation even while restore readiness is false."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT generation FROM privacy_state WHERE singleton = TRUE"
            ).fetchone()
            if row is None:
                raise RuntimeError("privacy state missing")
            return int(row[0])

    def _advance(self, conn: Any, generation: int) -> None:
        row = conn.execute(
            "UPDATE privacy_state SET generation = %s, updated_at = now() "
            "WHERE singleton = TRUE AND restore_ready = TRUE AND generation = %s",
            (generation, generation - 1),
        )
        if row.rowcount != 1:
            raise RuntimeError("privacy generation mismatch")

    def delete_terminal_and_advance(
        self, job_id: str, tenant_id: str, generation: int
    ) -> bool:
        with self._pool.connection() as conn:
            with conn.transaction():
                deleted = conn.execute(
                    "DELETE FROM jobs WHERE id = %s AND tenant_id = %s "
                    "AND status IN ('done', 'failed')",
                    (job_id, tenant_id),
                ).rowcount
                if deleted != 1:
                    raise RuntimeError("marked job not terminal")
                self._advance(conn, generation)
                return True

    def expired_terminal_jobs(
        self, *, retention_days: int, limit: int = 100
    ) -> list[tuple[str, str]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id::text, tenant_id::text FROM jobs "
                "WHERE status IN ('done', 'failed') "
                "AND created_at < now() - make_interval(days => %s) "
                "ORDER BY created_at, id LIMIT %s",
                (retention_days, limit),
            ).fetchall()
            return [(row[0], row[1]) for row in rows]

    def tenant_jobs(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                "SELECT id::text, status, filename, pages, result, error, "
                "attempts, created_at, updated_at, purged_at FROM jobs "
                "WHERE tenant_id = %s ORDER BY created_at, id",
                (tenant_id,),
            )
            return list(cur.fetchall())

    def active_tenant_jobs(self, tenant_id: str) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM jobs WHERE tenant_id = %s "
                "AND status IN ('pending', 'processing')", (tenant_id,)
            ).fetchone()
            return int(row[0]) if row else 0

    def delete_tenant_and_advance(self, tenant_id: str, generation: int) -> int:
        with self._pool.connection() as conn:
            with conn.transaction():
                deleted = conn.execute(
                    "DELETE FROM jobs WHERE tenant_id = %s", (tenant_id,)
                ).rowcount
                self._advance(conn, generation)
                return deleted

    def advance_privacy_generation(self, generation: int) -> None:
        with self._pool.connection() as conn:
            with conn.transaction():
                self._advance(conn, generation)

    def reconcile_erasures(
        self, tenants: set[str], jobs: set[tuple[str, str]], generation: int
    ) -> int:
        """After restore, replay every marker before promoting the database."""
        with self._pool.connection() as conn:
            with conn.transaction():
                deleted = 0
                for tenant_id in sorted(tenants):
                    deleted += conn.execute(
                        "DELETE FROM jobs WHERE tenant_id = %s", (tenant_id,)
                    ).rowcount
                for tenant_id, job_id in sorted(jobs):
                    deleted += conn.execute(
                        "DELETE FROM jobs WHERE id = %s AND tenant_id = %s",
                        (job_id, tenant_id),
                    ).rowcount
                updated = conn.execute(
                    "UPDATE privacy_state SET generation = %s, "
                    "restore_ready = TRUE, database_name = current_database(), "
                    "updated_at = now() "
                    "WHERE singleton = TRUE", (generation,)
                )
                if updated.rowcount != 1:
                    raise RuntimeError("privacy state missing")
                return deleted

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim one pending job. Returns None if queue empty."""
        with self._pool.connection() as conn:
            with conn.transaction():
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    """
                    SELECT id, tenant_id::text, storage_path, filename, attempts
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
                WHERE status IN ('done', 'failed')
                  AND purged_at IS NULL
                  AND (status = 'failed'
                       OR created_at < now() - make_interval(days => %s))
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

from __future__ import annotations

import logging
from importlib.resources import files

from psycopg_pool import ConnectionPool

from .config import Settings

logger = logging.getLogger("papela.db")

# Development/test bootstrap only. Production schema changes are operator-run.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id           UUID PRIMARY KEY,
    tenant_id    UUID NOT NULL,
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
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- LGPD audit: when the raw PDF was deleted from disk (NULL = still present
    -- or never stored). Kept even after the file is gone as proof of erasure.
    purged_at    TIMESTAMPTZ
);
-- Idempotent add for tables created before purged_at existed.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ;
-- Partial index: the worker only ever scans pending rows.
CREATE INDEX IF NOT EXISTS idx_jobs_pending
    ON jobs (created_at) WHERE status = 'pending';
-- Partial index for the retention sweep: done jobs whose file isn't purged yet.
CREATE INDEX IF NOT EXISTS idx_jobs_purge
    ON jobs (created_at) WHERE status = 'done' AND purged_at IS NULL;
"""


class Database:
    def __init__(self, settings: Settings) -> None:
        self._production = settings.env == "production"
        self._pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            open=False,
            kwargs={
                "autocommit": True,
                "connect_timeout": 5,
                "options": "-c statement_timeout=10000 -c lock_timeout=5000",
            },
        )

    def open(self) -> None:
        self._pool.open()
        try:
            self._pool.wait(timeout=10.0)
            with self._pool.connection() as conn:
                if self._production:
                    # Schema is applied separately by its owner. Runtime must
                    # not own/alter schema, create roles/DBs, or bypass RLS.
                    flags = conn.execute(
                        "SELECT rolsuper OR rolcreaterole OR rolcreatedb "
                        "OR rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    ).fetchone()
                    owner = conn.execute(
                        "SELECT tableowner FROM pg_tables "
                        "WHERE schemaname = 'public' AND tablename = 'jobs'"
                    ).fetchone()
                    can_create = conn.execute(
                        "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
                    ).fetchone()
                    owner_membership = (
                        conn.execute(
                            "SELECT pg_has_role(current_user, %s, 'MEMBER')",
                            (owner[0],),
                        ).fetchone()
                        if owner else None
                    )
                    if (
                        not flags
                        or flags[0]
                        or not owner
                        or not can_create
                        or can_create[0]
                        or not owner_membership
                        or owner_membership[0]
                    ):
                        raise RuntimeError(
                            "Production requires provisioned least-privilege DB role"
                        )
                    tenant_column = conn.execute(
                        "SELECT attnotnull FROM pg_attribute "
                        "WHERE attrelid = to_regclass('public.jobs') "
                        "AND attname = 'tenant_id' AND NOT attisdropped"
                    ).fetchone()
                    if not tenant_column or not tenant_column[0]:
                        raise RuntimeError(
                            "Production requires tenant isolation migration"
                        )
                    immutable_trigger = conn.execute(
                        "SELECT 1 FROM pg_trigger "
                        "WHERE tgrelid = to_regclass('public.jobs') "
                        "AND tgname = 'jobs_tenant_immutable' "
                        "AND tgenabled IN ('O', 'A') AND NOT tgisinternal"
                    ).fetchone()
                    if not immutable_trigger:
                        raise RuntimeError(
                            "Production requires immutable tenant ownership"
                        )
                else:
                    conn.execute(SCHEMA_SQL)
                    migration = (
                        files("app")
                        .joinpath("migrations/0002_tenant_isolation.sql")
                        .read_text(encoding="utf-8")
                    )
                    with conn.transaction():
                        conn.execute(migration)
        except Exception:
            self._pool.close()
            raise
        logger.info("db.ready", extra={"request_id": None})

    def close(self) -> None:
        self._pool.close()

    def is_ready(self) -> bool:
        """Bounded, read-only check for routing new requests to this API."""
        try:
            with self._pool.connection(timeout=2.0) as conn:
                return conn.execute("SELECT 1").fetchone() == (1,)
        except Exception:
            return False

    @property
    def pool(self) -> ConnectionPool:
        return self._pool

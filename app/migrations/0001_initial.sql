-- Explicit production bootstrap. Run only through the single-shot migration
-- command, as the schema owner, before starting the API or worker.
CREATE TABLE IF NOT EXISTS public.jobs (
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
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    purged_at    TIMESTAMPTZ
);
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_jobs_pending
    ON public.jobs (created_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_jobs_purge
    ON public.jobs (created_at) WHERE status = 'done' AND purged_at IS NULL;

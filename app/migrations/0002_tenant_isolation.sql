-- Run once as the schema owner before starting a production API/worker:
-- psql "$MIGRATION_DATABASE_URL" -X -v ON_ERROR_STOP=1 -1 -f app/migrations/0002_tenant_isolation.sql
-- Never infer the customer owner of historical shared-trust jobs. Preserve
-- those rows under a reserved, API-inaccessible quarantine tenant until a
-- separately reviewed ownership assignment can be made.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE jobs
SET tenant_id = '00000000-0000-0000-0000-000000000001'
WHERE tenant_id IS NULL;
ALTER TABLE jobs ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_tenant_created
    ON jobs (tenant_id, created_at DESC);

-- The application never changes ownership; make that invariant independent
-- of endpoint implementation. Reassignment of quarantined historical jobs
-- requires a separate, reviewed owner-level migration.
CREATE OR REPLACE FUNCTION jobs_reject_tenant_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
        RAISE EXCEPTION 'job tenant ownership is immutable';
    END IF;
    RETURN NEW;
END;
$$;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'jobs'::regclass
          AND tgname = 'jobs_tenant_immutable'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER jobs_tenant_immutable
        BEFORE UPDATE ON jobs
        FOR EACH ROW EXECUTE FUNCTION jobs_reject_tenant_change();
    END IF;
END;
$$;

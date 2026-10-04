-- A restored snapshot carries its old generation. Readiness compares it to
-- the independent erasure journal and fails closed until reconciliation.
CREATE TABLE IF NOT EXISTS public.privacy_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    restore_ready BOOLEAN NOT NULL DEFAULT TRUE,
    database_name TEXT NOT NULL DEFAULT current_database(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE public.privacy_state
    ADD COLUMN IF NOT EXISTS restore_ready BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE public.privacy_state
    ADD COLUMN IF NOT EXISTS database_name TEXT NOT NULL DEFAULT current_database();
INSERT INTO public.privacy_state (singleton, generation, database_name)
VALUES (TRUE, 0, current_database()) ON CONFLICT (singleton) DO NOTHING;

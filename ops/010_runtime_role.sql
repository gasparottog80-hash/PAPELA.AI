-- psql reads the password from the migration container's environment. The
-- value is never echoed by this script or stored in a repository file.
\getenv runtime_password PAPELA_RUNTIME_PASSWORD
SELECT format('CREATE ROLE papela_runtime LOGIN PASSWORD %L', :'runtime_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'papela_runtime') \gexec
SELECT format(
    'ALTER ROLE papela_runtime WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
    :'runtime_password'
) \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO papela_runtime;
REVOKE ALL ON public.jobs FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.jobs TO papela_runtime;
REVOKE ALL ON public.privacy_state FROM PUBLIC;
GRANT SELECT, UPDATE ON public.privacy_state TO papela_runtime;
REVOKE ALL ON FUNCTION public.jobs_reject_tenant_change() FROM PUBLIC;

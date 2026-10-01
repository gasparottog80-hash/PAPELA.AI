#!/bin/sh
set -eu

# This is an explicit, one-shot operator command, never an app entrypoint.
# No xtrace, command-line password or secret echo. All scripts run in one
# transaction under a transaction-scoped advisory lock.
PGPASSWORD="$(cat /run/secrets/pg_admin_password)"
PAPELA_RUNTIME_PASSWORD="$(cat /run/secrets/pg_runtime_password)"
if [ "${#PGPASSWORD}" -lt 24 ] || [ "${#PAPELA_RUNTIME_PASSWORD}" -lt 24 ]; then
    echo 'Production database passwords must be at least 24 characters' >&2
    exit 1
fi
export PGPASSWORD PAPELA_RUNTIME_PASSWORD
export PGCONNECT_TIMEOUT=5
export PGOPTIONS='-c statement_timeout=600000 -c lock_timeout=5000 -c search_path=public'

psql -X -q -1 -v ON_ERROR_STOP=1 -h postgres -U papela_owner -d papela \
    -f /opt/papela/0000_lock.sql \
    -f /opt/papela/0001_initial.sql \
    -f /opt/papela/0002_tenant_isolation.sql \
    -f /opt/papela/010_runtime_role.sql
echo 'Production schema and runtime role are ready'

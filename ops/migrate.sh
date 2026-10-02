#!/bin/sh
set -eu

# This is an explicit, one-shot operator command, never an app entrypoint.
# No xtrace, command-line password or secret echo. All scripts run in one
# transaction under a transaction-scoped advisory lock.
uptime_ms() { awk '{printf "%.0f", $1 * 1000}' /proc/uptime; }
started_ms="$(uptime_ms)"
timestamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
printf '{"ts":"%s","level":"INFO","service":"migrate","event":"migration.started"}\n' "$(timestamp)"
PGPASSWORD="$(cat /run/secrets/pg_admin_password)"
PAPELA_RUNTIME_PASSWORD="$(cat /run/secrets/pg_runtime_password)"
if [ "${#PGPASSWORD}" -lt 24 ] || [ "${#PAPELA_RUNTIME_PASSWORD}" -lt 24 ]; then
    printf '{"ts":"%s","level":"ERROR","service":"migrate","event":"migration.failed","error_code":"MIGRATION_CONFIG_INVALID"}\n' "$(timestamp)" >&2
    exit 1
fi
export PGPASSWORD PAPELA_RUNTIME_PASSWORD
export PGCONNECT_TIMEOUT=5
export PGOPTIONS='-c statement_timeout=600000 -c lock_timeout=5000 -c search_path=public'

if psql -X -q -1 -v ON_ERROR_STOP=1 -h postgres -U papela_owner -d papela \
    -f /opt/papela/0000_lock.sql \
    -f /opt/papela/0001_initial.sql \
    -f /opt/papela/0002_tenant_isolation.sql \
    -f /opt/papela/010_runtime_role.sql >/dev/null 2>&1; then
    duration_ms=$(( $(uptime_ms) - started_ms ))
    printf '{"ts":"%s","level":"INFO","service":"migrate","event":"migration.completed","duration_ms":%s}\n' "$(timestamp)" "$duration_ms"
else
    result=$?
    printf '{"ts":"%s","level":"ERROR","service":"migrate","event":"migration.failed","error_code":"MIGRATION_FAILED","exit_status":%s}\n' "$(timestamp)" "$result" >&2
    exit "$result"
fi

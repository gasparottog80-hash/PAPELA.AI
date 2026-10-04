#!/bin/sh
set -eu
umask 077

# Run only in the one-shot backup container. No password appears in argv,
# stdout, Dockerfile layers, the backup directory or the archive metadata.
backup_root=/backups
db_host=postgres
db_name=papela
db_user=papela_owner
stage=
operation=${1:-}
case "$operation" in
    backup|verify|retention|retention-age) operation_event=$operation ;;
    restore-test) operation_event=restore_test ;;
    *) operation_event=backup ;;
esac
started_ms=$(awk '{printf "%.0f", $1 * 1000}' /proc/uptime)

timestamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
elapsed_ms() { now=$(awk '{printf "%.0f", $1 * 1000}' /proc/uptime); echo $((now - started_ms)); }
event() {
    if [ "$3" = NONE ]; then
        printf '{"ts":"%s","level":"%s","service":"backup","event":"%s","duration_ms":%s}\n' \
            "$(timestamp)" "$1" "$2" "$(elapsed_ms)"
    else
        printf '{"ts":"%s","level":"%s","service":"backup","event":"%s","duration_ms":%s,"error_code":"%s"}\n' \
            "$(timestamp)" "$1" "$2" "$(elapsed_ms)" "$3"
    fi
}
fail() {
    event ERROR "${operation_event}_failed" "$1" >&2
    exit 1
}
cleanup() {
    # The only recursive deletion here is a newly-created, unpublished
    # staging directory. Never delete the backup root or a completed backup.
    case "$stage" in
        /backups/.partial-backup-*) [ ! -e "$stage" ] || rm -r -- "$stage" ;;
    esac
}
trap cleanup EXIT

valid_id() {
    printf '%s\n' "$1" | grep -Eq '^backup-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$'
}
require_root() {
    [ -d "$backup_root" ] && [ ! -L "$backup_root" ] && [ -w "$backup_root" ] \
        || fail BACKUP_DIRECTORY_UNAVAILABLE
}
load_password() {
    [ -r /run/secrets/pg_admin_password ] || fail BACKUP_CREDENTIAL_UNAVAILABLE
    PGPASSWORD=$(cat /run/secrets/pg_admin_password)
    [ "${#PGPASSWORD}" -ge 24 ] || fail BACKUP_CREDENTIAL_INVALID
    export PGPASSWORD
    export PGCONNECT_TIMEOUT=5
}
verify_archive() {
    id=$1
    valid_id "$id" || fail BACKUP_ID_INVALID
    dir="$backup_root/$id"
    [ -d "$dir" ] && [ ! -L "$dir" ] || fail BACKUP_NOT_FOUND
    for file in database.dump database.sha256 metadata.json; do
        [ -f "$dir/$file" ] && [ ! -L "$dir/$file" ] || fail BACKUP_INCOMPLETE
    done
    bytes=$(wc -c < "$dir/database.dump")
    [ "$bytes" -gt 0 ] || fail BACKUP_EMPTY
    (cd "$dir" && sha256sum -c database.sha256 >/dev/null 2>&1) \
        || fail BACKUP_CHECKSUM_MISMATCH
    pg_restore --list "$dir/database.dump" >/dev/null 2>&1 \
        || fail BACKUP_ARCHIVE_INVALID
    grep -q '"format":"pg_dump_custom_v1"' "$dir/metadata.json" \
        || fail BACKUP_METADATA_INVALID
}
do_backup() {
    [ "$#" -eq 0 ] || fail BACKUP_ARGUMENT_INVALID
    require_root
    load_password
    event INFO backup_started NONE
    stamp=$(date -u '+%Y%m%dT%H%M%SZ')
    created_at=$(timestamp)
    nonce=$(cut -c1-8 /proc/sys/kernel/random/uuid)
    id="backup-$stamp-$nonce"
    final="$backup_root/$id"
    [ ! -e "$final" ] || fail BACKUP_ALREADY_EXISTS
    stage=$(mktemp -d "$backup_root/.partial-backup-XXXXXX") \
        || fail BACKUP_STAGING_FAILED
    chmod 700 "$stage"
    # A privacy marker can commit during pg_dump. Generations are monotonic;
    # equal values before/after prove the dump did not cross an erasure.
    privacy_before=$(psql -X -Atq --host="$db_host" --username="$db_user" \
        --dbname="$db_name" -c 'SELECT generation FROM public.privacy_state WHERE singleton=TRUE' 2>/dev/null) \
        || fail BACKUP_PRIVACY_STATE_UNAVAILABLE
    case "$privacy_before" in *[!0-9]*|'') fail BACKUP_PRIVACY_STATE_UNAVAILABLE ;; esac
    pg_dump --host="$db_host" --username="$db_user" --dbname="$db_name" \
        --format=custom --compress=6 --no-password \
        --file="$stage/database.dump" >/dev/null 2>&1 \
        || fail BACKUP_DUMP_FAILED
    [ "$(wc -c < "$stage/database.dump")" -gt 0 ] || fail BACKUP_EMPTY
    pg_restore --list "$stage/database.dump" >/dev/null 2>&1 \
        || fail BACKUP_ARCHIVE_INVALID
    privacy_after=$(psql -X -Atq --host="$db_host" --username="$db_user" \
        --dbname="$db_name" -c 'SELECT generation FROM public.privacy_state WHERE singleton=TRUE' 2>/dev/null) \
        || fail BACKUP_PRIVACY_STATE_UNAVAILABLE
    [ "$privacy_before" = "$privacy_after" ] || fail BACKUP_PRIVACY_GENERATION_CHANGED
    (cd "$stage" && sha256sum database.dump > database.sha256) \
        || fail BACKUP_CHECKSUM_FAILED
    (cd "$stage" && sha256sum -c database.sha256 >/dev/null 2>&1) \
        || fail BACKUP_CHECKSUM_FAILED
    server_version=$(psql -X -Atq --host="$db_host" --username="$db_user" \
        --dbname="$db_name" -c 'SHOW server_version_num' 2>/dev/null) \
        || fail BACKUP_METADATA_FAILED
    case "$server_version" in *[!0-9]*|'') fail BACKUP_METADATA_FAILED ;; esac
    bytes=$(wc -c < "$stage/database.dump")
    printf '{"format":"pg_dump_custom_v1","created_at_utc":"%s","source_database":"%s","schema":"jobs-tenant-v3","privacy_generation":%s,"postgres_version_num":%s,"bytes":%s}\n' \
        "$created_at" "$db_name" "$privacy_after" "$server_version" "$bytes" > "$stage/metadata.json"
    chmod 600 "$stage/database.dump" "$stage/database.sha256" "$stage/metadata.json"
    mv -- "$stage" "$final" || fail BACKUP_PUBLISH_FAILED
    stage=
    verify_archive "$id"
    event INFO backup_succeeded NONE
    printf 'BACKUP_ID=%s\n' "$id"
}
do_verify() {
    [ "$#" -eq 1 ] || fail BACKUP_ARGUMENT_INVALID
    require_root
    verify_archive "$1"
    event INFO backup_verified NONE
}
do_restore() {
    [ "$#" -eq 2 ] || fail RESTORE_ARGUMENT_INVALID
    require_root
    id=$1
    target=$2
    # Deliberately no in-place production restore path. A new, explicitly
    # named database must already exist and be empty before restore.
    printf '%s\n' "$target" | grep -Eq '^papela_restore_[a-z0-9_]{1,45}$' \
        || fail RESTORE_TARGET_FORBIDDEN
    verify_archive "$id"
    load_password
    # Connecting to the exact target establishes existence; there is no
    # dynamic SQL containing an operator-controlled database name.
    objects=$(psql -X -Atq --host="$db_host" --username="$db_user" \
        --dbname="$target" -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f')" 2>/dev/null) \
        || fail RESTORE_TARGET_CHECK_FAILED
    [ "$objects" = 0 ] || fail RESTORE_TARGET_NOT_EMPTY
    event INFO restore_test_started NONE
    pg_restore --host="$db_host" --username="$db_user" --dbname="$target" \
        --no-password --no-owner --exit-on-error --single-transaction \
        "$backup_root/$id/database.dump" >/dev/null 2>&1 \
        || fail RESTORE_TEST_FAILED
    schema=$(psql -X -Atq --host="$db_host" --username="$db_user" \
        --dbname="$target" -c "SELECT (to_regclass('public.jobs') IS NOT NULL)::int, count(*) FROM pg_attribute WHERE attrelid='public.jobs'::regclass AND attname='tenant_id' AND attnotnull GROUP BY 1" 2>/dev/null) \
        || fail RESTORE_SCHEMA_INVALID
    [ "$schema" = '1|1' ] || fail RESTORE_SCHEMA_INVALID
    owner=$(psql -X -Atq --host="$db_host" --username="$db_user" \
        --dbname="$target" -c "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.jobs'::regclass" 2>/dev/null) \
        || fail RESTORE_OWNERSHIP_INVALID
    [ "$owner" = papela_owner ] || fail RESTORE_OWNERSHIP_INVALID
    isolation=$(psql -X -Atq --host="$db_host" --username="$db_user" \
        --dbname="$target" -c "SELECT (SELECT count(*) FROM pg_trigger WHERE tgrelid='public.jobs'::regclass AND tgname='jobs_tenant_immutable' AND NOT tgisinternal), has_table_privilege('papela_runtime','public.jobs','SELECT'), has_table_privilege('papela_runtime','public.jobs','INSERT')" 2>/dev/null) \
        || fail RESTORE_ISOLATION_INVALID
    [ "$isolation" = '1|t|t' ] || fail RESTORE_ISOLATION_INVALID
    # The transient PDF volume is intentionally not backed up. Never let a
    # recovered worker retry a job whose source PDF is absent; clients must
    # resubmit these jobs. Completed JSON results remain intact.
    psql -X -q --host="$db_host" --username="$db_user" --dbname="$target" \
        -v ON_ERROR_STOP=1 -c "UPDATE public.jobs SET status='failed', error='processing_error: RestoreRequiresResubmission', purged_at=now(), updated_at=now() WHERE status IN ('pending','processing')" \
        >/dev/null 2>&1 || fail RESTORE_RECONCILIATION_FAILED
    # A restored snapshot is never promotable until the independent erasure
    # journal has been validated and replayed by the Gate 8 operator command.
    psql -X -q --host="$db_host" --username="$db_user" --dbname="$target" \
        -v ON_ERROR_STOP=1 -c "UPDATE public.privacy_state SET restore_ready=FALSE WHERE singleton=TRUE" \
        >/dev/null 2>&1 || fail RESTORE_ERASURE_GATE_FAILED
    event INFO restore_test_succeeded NONE
}
do_retention() {
    [ "$#" -eq 2 ] || fail RETENTION_ARGUMENT_INVALID
    require_root
    new_id=$1
    keep=$2
    case "$keep" in *[!0-9]*|'') fail RETENTION_ARGUMENT_INVALID ;; esac
    [ "$keep" -ge 1 ] && [ "$keep" -le 365 ] || fail RETENTION_ARGUMENT_INVALID
    verify_archive "$new_id"
    ids=$(for path in "$backup_root"/backup-*; do
        [ -d "$path" ] && [ ! -L "$path" ] || continue
        name=${path##*/}
        valid_id "$name" && printf '%s\n' "$name"
    done | sort -r)
    latest=$(printf '%s\n' "$ids" | head -n 1)
    [ "$latest" = "$new_id" ] || fail RETENTION_REQUIRES_NEWEST_VERIFIED
    printf '%s\n' "$ids" | tail -n "+$((keep + 1))" | while IFS= read -r old_id; do
        [ -n "$old_id" ] && valid_id "$old_id" || continue
        [ "$old_id" != "$new_id" ] || fail RETENTION_GUARD_FAILED
        rm -r -- "$backup_root/$old_id" || fail RETENTION_DELETE_FAILED
    done
    event INFO retention_succeeded NONE
}
do_retention_age() {
    [ "$#" -eq 1 ] || fail RETENTION_ARGUMENT_INVALID
    require_root
    new_id=$1
    days=${PAPELA_BACKUP_RETENTION_DAYS:-30}
    case "$days" in *[!0-9]*|'') fail RETENTION_ARGUMENT_INVALID ;; esac
    [ "$days" -ge 1 ] && [ "$days" -le 3650 ] || fail RETENTION_ARGUMENT_INVALID
    verify_archive "$new_id"
    ids=$(for path in "$backup_root"/backup-*; do
        [ -d "$path" ] && [ ! -L "$path" ] || continue
        name=${path##*/}
        valid_id "$name" && printf '%s\n' "$name"
    done | sort -r)
    latest=$(printf '%s\n' "$ids" | head -n 1)
    [ "$latest" = "$new_id" ] || fail RETENTION_REQUIRES_NEWEST_VERIFIED
    now=$(date -u '+%s')
    cutoff=$((now - days * 86400))
    # Preserve the newest two completed copies even when both are older than
    # the technical age window. Their own archive integrity is still checked
    # by the ordinary backup/verify workflow.
    printf '%s\n' "$ids" | tail -n +3 | while IFS= read -r old_id; do
        [ -n "$old_id" ] && valid_id "$old_id" || continue
        modified=$(stat -c '%Y' "$backup_root/$old_id") \
            || fail RETENTION_STAT_FAILED
        case "$modified" in *[!0-9]*|'') fail RETENTION_STAT_FAILED ;; esac
        if [ "$modified" -lt "$cutoff" ]; then
            rm -r -- "$backup_root/$old_id" || fail RETENTION_DELETE_FAILED
        fi
    done
    event INFO retention_age_succeeded NONE
}

case "$operation" in
    backup) shift; do_backup "$@" ;;
    verify) shift; do_verify "$@" ;;
    restore-test) shift; do_restore "$@" ;;
    retention) shift; do_retention "$@" ;;
    retention-age) shift; do_retention_age "$@" ;;
    *) fail BACKUP_ACTION_INVALID ;;
esac

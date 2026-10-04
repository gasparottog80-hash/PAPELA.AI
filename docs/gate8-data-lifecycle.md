# Gate 8 — local/CI data lifecycle runbook

This candidate is tested **only with synthetic tenants and documents**. None
of the commands below authorizes operations against customer data or a live
database. No legal retention period or LGPD compliance is asserted. Obtain
business/legal decisions, operator approval and an independently tested
journal backup before any production use.

## Boundary and ordering

The independent `PAPELA_ERASURE_DIR` must be a host bind outside the Postgres
volume, snapshots and backups. It and its `events/` directory are mode `0700`;
files are `0600`; the API/worker UID must own them. Never bind a fresh empty
journal over an existing database. Production does not auto-initialize it.
Provision it once, then back it up separately; restore it **before** starting
API/worker. Do not edit, truncate or move journal files individually. The
index and every event are validated on reads. The checksums detect accidental
damage, not a malicious actor with write access. Directory permission, backup
integrity, restricted access and recovery testing are separate obligations.

Customer `DELETE /v1/jobs/{id}` accepts only an authenticated owner's terminal
job. Cross-tenant access is 404; active jobs are 409. A repeated owner delete
is 204. The handler validates owner and journal, writes/fsyncs the marker,
removes any PDF, removes the row/result, advances the database generation and
emits a content-free audit event. If marker persistence or later cleanup fails,
readiness fails closed until reconciliation. The worker's configurable
`PAPELA_JOB_RETENTION_DAYS` (technical default 30) runs the same sequence for
expired terminal jobs; `PAPELA_PURGE_INTERVAL_S` defaults to 3600 seconds.
Pending/processing jobs are not aged out. Raw PDF expiry has the separate
`PAPELA_RETENTION_DAYS` fallback (technical default 7).

Operator actions are CLI-only, not admin HTTP routes. On a synthetic local
database, run `python -m app.lifecycle {init|export|offboard|reconcile|revoke-key|record-key-rotation}`
with `PAPELA_ALLOW_PRIVACY_OPS=1` and `--execute`. All tenant actions require
canonical UUID `--tenant` and identical `--confirm-tenant`; export also
requires a new absolute output in an existing private directory. Export writes
one tenant's structured job/result JSON, excluding internal paths, keys and
logs, with exclusive creation and mode `0600` where supported. Do not point it
at a shared, symlinked or world-readable directory. Treat the output as
sensitive and account for its secure transfer, retention and disposal outside
the app. No customer self-service or IAM is created.

Offboarding writes the tenant marker first, then deletes all active PDFs and
rows; no new upload can commit after the marker, and an in-flight worker cannot
persist a completed result after deletion. Validate zero rows and 401 access
for the tenant, while another synthetic tenant remains usable. The external
key file still needs operator removal and API recreation. A crashed operation
must be reconciled before any traffic resumes. Never remove markers to make a
tenant appear active again; re-onboarding requires a distinct tenant UUID.

## Restores and credentials

Gate 6 `restore-test` marks the isolated target database
`privacy_state.restore_ready=FALSE`. The saved database name must also match
`current_database()`; a clone restored under a new name fails closed even if
someone bypasses the sanctioned restore tool. Keep API/worker stopped; verify the target
is a **new, isolated** `papela_restore_*` database and that the complete,
independent journal is available. The CLI `reconcile` action requires exact
`--database` and `--confirm-database` names, validates every marker, replays
tenant/job revocations, and only then sets `restore_ready=TRUE` and the latest
generation. A corrupt/missing journal or mismatch keeps `/readiness` at 503
and `/v1` unavailable. Run a synthetic extraction and cross-tenant negative
checks before any future promotion. The CI regression explicitly performs
backup T0, deletion/offboarding T1, restore T0, blocked readiness, replay,
then confirms no resurrection. Never use a restored DB as a shortcut to
recover intentionally deleted customer data.

Provision a tenant with a unique high-entropy API key in the external secret
file; never write the key to Git or logs. To rotate, replace the value in that
file atomically, recreate API, verify old key rejected and new key admitted,
then record `record-key-rotation` for the tenant. For revocation, first record
`revoke-key` (immediate tenant denylist), then remove the key from the external
file and recreate API; verify 401 and no other tenant impact. Those events
contain tenant UUID only, not key material. The command records an operational
event; it does **not** edit the external secret. Record approval and evidence
outside application logs. Production operator identity/access is not yet
implemented beyond host access and explicit CLI confirmation.

## Backup inventory and conservative compaction (synthetic only)

`backup-inventory.json` lives beside the journal index, outside Postgres and
its snapshots. It is an atomically published, checksummed JSON document (not
an authenticated signature). `coverage` names the source database, schema and
restricted local backup root,
asserts that **both local and offsite listings are complete**, and records a
UTC verification time. Every unique `backup_id` has UTC creation/expiry and
verification times, source database/schema, privacy generation captured in
the DB snapshot, SHA-256 of the archive, logical location, `local`/`offsite`
class, `present`/`expired`/`deleted`/`invalid` status, legal hold and marker
horizon. The horizon must equal the captured generation. A local `present`
archive is rehashed on validation; all local `*.dump` files beneath the root
must appear in the inventory. `expired` and `deleted` require the archive to
be absent (calendar expiry alone is insufficient). Unknown, invalid or
duplicate entries, absent/incomplete coverage and corrupt JSON fail closed.
API and worker must be able to read that same local root when an inventory is
published; the synthetic drill mounts it read-only. Current production Compose
does **not** mount the backup root into API/worker, so publishing an inventory
there would deliberately make readiness 503. Do not enable production
inventory/compaction before the backup topology and access model are reviewed.

The operator command `python -m app.lifecycle compact` is a dry-run by
default and reports each marker sequence, type, eligibility and reason without
tenant/job IDs. Mutation additionally requires `--execute`,
`PAPELA_ALLOW_PRIVACY_OPS=1`, `PAPELA_ENV=test`, the exact database twice via
`--database`/`--confirm-database`, and the resolved journal directory via
`--confirm-journal`. It is deliberately **not executable in production**.
Before mutation, stop API/worker in the synthetic drill. The command only
removes job/result markers when the DB and journal generations match, every
backup with an older captured generation is verifiably expired/deleted, the
inventory covers the marker, and no hold or offsite uncertainty exists.
Tenant disable/delete/revocation markers remain to prevent credential or
tenant-ID reuse. Offsite entries always block pruning until an independently
verified provider receipt/integration is designed; no S3 support is implied.

The compactor writes a new checksummed index, fsyncs and atomically replaces
the old index. It temporarily lists removable event files as hashed garbage;
an interruption remains valid and a later run cleans them up. It then unlinks
those exact files, fsyncs the directory and atomically publishes an index
without garbage. `floor_generation` records the newest pruned sequence.
A restored DB below that floor is rejected, even if its old archive was later
reintroduced; a reintroduced old backup also makes readiness false. A valid
newer restore still reconciles retained markers. This floor is deliberately
conservative and may make additional old snapshots unrestorable.

Backup retention and journal retention are **different**. `retention-age`
sets a technical backup window but never proves physical destruction or
offsite absence. Journal retention follows verified backup state, not TTL.
With no complete inventory/policy, markers remain. SHA-256 and operator
attestation do not prove that unregistered copies do not exist; production
needs independently audited inventory generation, backup deletion receipts,
offsite coverage, legal-hold process and restore drills before compaction can
be enabled. No real backup or customer data is deleted by this feature.

## Retention limits and gaps

The backup tool has `retention-age <newest-verified-backup-id>` with
`PAPELA_BACKUP_RETENTION_DAYS` (technical default 30), keeping the newest two
verified backups; count-based retention remains. These are explicit operations,
not a scheduler or legal hold system. The journal cannot be pruned by age
alone while a restorable backup predates a marker. The test-only compactor
above is disabled for production, so production markers remain indefinitely
for this candidate. Back up the journal and inventory independently for
longer than the oldest recoverable DB snapshot; losing either after
compaction must block recovery.

Docker `json-file` logs are size/count-rotated via `PAPELA_LOG_MAX_SIZE` and
`PAPELA_LOG_MAX_FILES`, but Compose does not provide an age-based purge. A
host/collector policy, legal hold and test of time-based deletion remain
necessary. Logs allowlist only event type, request/job IDs, safe counts and a
hashed subject reference; avoid document/key canaries in any log sink. Metrics
are aggregate and in memory. Neither log rotation nor journal retention is
presented as a final legal policy.

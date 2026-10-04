# Gate 6 — backup and restore runbook

This is a tested **local/CI recovery procedure**, not a production deploy or
permission to accept customer traffic. The backup contains sensitive fiscal
results in plaintext custom PostgreSQL format. Provision encrypted host storage,
restricted operators and an offsite copy before relying on it for real disaster
recovery. Never put an archive, secret, key or customer document in Git, CI
artifacts or a public image.

## Scope and consistency

| Resource | Persistent | Policy | Reason |
| --- | --- | --- | --- |
| Postgres `pgdata` | Yes | Daily logical backup; keep seven verified local copies initially | Authoritative tenants, jobs and extracted results |
| Private `pdfs` volume | Yes, transient | Excluded | Raw uploads are purged after processing; an in-flight job cannot be reconstructed from a database dump alone |
| `caddy_data` | Yes | Separate encrypted copy optional | Contains ACME account/certificate private keys; can instead be reissued, with possible downtime/rate limits |
| `caddy_config` | Yes | Excluded | Recreated from versioned Caddyfile and deployment settings |
| Four external secret files | Yes | Separate secure escrow/rotation, **never** in the ordinary dump | Restore requires the matching DB/tenant credentials; file-backed Compose secrets are not encrypted storage |
| Images, Git source, caches, logs | Rebuildable or disposable | Excluded | CI rebuilds code/images; logs are privacy-minimized operational data, not business records |

`pg_dump` takes a consistent snapshot even while writes continue. The custom
archive is compressed at PostgreSQL level 6. It covers one database, **not**
cluster-global roles/passwords; on a fresh cluster initialize Postgres and run
the controlled migrator before restoring into a new database. The migrator
creates `papela_runtime` and its restricted grants. [PostgreSQL's `pg_dump`
documentation](https://www.postgresql.org/docs/16/app-pgdump.html) documents
the snapshot and one-database scope.

Because the transient PDF volume is excluded, restore marks any saved
`pending`/`processing` job `failed` with the safe code
`processing_error: RestoreRequiresResubmission` and stamps `purged_at`. Clients
must resubmit those documents. Completed results remain in Postgres. This is
an explicit availability/data-loss tradeoff, not an assertion that an in-flight
PDF can be recovered. Never point a recovered worker at an old, unrelated PDF
volume. Gate 8 must still settle the legal retention/exclusion policy for
extracted JSON and backups.

## Backup, verify, list and retention

Provision `PAPELA_BACKUP_DIR` **outside** the Git checkout on a host filesystem
with encryption at rest, sufficient space and operator-controlled access.
For Linux file-backed Compose mounts, make the directory owned by UID 70 and
mode `0700`; produced directories are `0700`, archive/checksum/metadata files
`0600`. The backup container runs as UID 70, read-only except `/backups` and a
small tmpfs, on the internal database network. It receives only the Postgres
owner password secret. No password is in argv or its JSON logs. `pg_dump`
failures suppress raw stderr and return nonzero with a bounded error code.

Use the same external production env file on **every** Compose invocation:

```sh
docker compose --env-file /secure/.env.production -f docker-compose.prod.yml config --quiet
docker compose --env-file /secure/.env.production -f docker-compose.prod.yml build backup
docker compose --env-file /secure/.env.production -f docker-compose.prod.yml --profile ops run --rm backup backup
# Save the emitted BACKUP_ID; example only:
docker compose --env-file /secure/.env.production -f docker-compose.prod.yml --profile ops run --rm backup verify backup-YYYYMMDDTHHMMSSZ-aaaaaaaa
docker compose --env-file /secure/.env.production -f docker-compose.prod.yml --profile ops run --rm backup retention backup-YYYYMMDDTHHMMSSZ-aaaaaaaa 7
```

Schedule this **sequence** daily only after deployment approval; no cron or
timer was installed in this gate. The seven-copy default is a short local
operational window to detect a bad backup without unbounded disk growth, **not**
a legal retention period. The final numeric argument configures the count
(`1..365`). Retention refuses to run unless its supplied backup is the newest
and passes checksum and archive checks. It never removes that backup. Monitor
capacity; the policy does not protect against a failed host or ransomware.

Each archive lives in a UTC/nonce-named directory. A hidden staging directory
is populated, checked for nonzero size, checked with `pg_restore --list`, given
a SHA-256 file and minimal metadata, then renamed atomically as one directory.
An interrupted staging directory is removed; an unverified dump is not
published as a completed backup. `verify` recalculates SHA-256 and checks the
PostgreSQL archive TOC. Do not assume that existence of a file means it is
restorable. The actual restore drill below is the stronger proof.

An operator can list completed IDs without opening their contents:

```sh
find /srv/papela/backups -maxdepth 1 -type d -name 'backup-*' -print
```

The metadata records UTC creation time, PostgreSQL server version, a schema
hint and archive bytes, never tenant IDs, filenames or document values.

## Restore drill and controlled real recovery

**Never restore in place over `papela`.** The tool accepts only an explicitly
named, existing, empty `papela_restore_*` database. It checks the archive
before connecting to the target, restores with `pg_restore --single-transaction
--exit-on-error --no-owner`, and validates the jobs table, non-null tenant ID,
immutable-tenant trigger, table ownership and runtime grants. A failure leaves
the application pointed at the original database. [PostgreSQL's `pg_restore`
documentation](https://www.postgresql.org/docs/16/app-pgrestore.html) describes
archive listing and single-transaction restore.

For an isolated drill, create a disposable empty target with a reviewed name:

```sh
docker compose --env-file /secure/.env.production -f docker-compose.prod.yml exec -T postgres \
  createdb -U papela_owner papela_restore_yyyymmdd
docker compose --env-file /secure/.env.production -f docker-compose.prod.yml --profile ops run --rm backup \
  restore-test backup-YYYYMMDDTHHMMSSZ-aaaaaaaa papela_restore_yyyymmdd
```

Use synthetic data for recurring drills. `restore-test` leaves
`privacy_state.restore_ready=FALSE`; the saved database name also differs from
the isolated target. API/worker must remain stopped until the separate Gate 8
erasure journal has been validated and replayed with the operator-only
`reconcile` command. Inspect aggregate row and tenant counts, trigger/grants,
API cross-tenant 404s, `/health`, `/readiness`, and a newly processed job only
**after** reconciliation. The CI drill additionally deletes a job and
offboards a synthetic tenant after the T0 backup, then proves neither is
resurrected by restore. See [Gate 8 operations](gate8-data-lifecycle.md).

For a real incident, first preserve the failed system and identify the last
verified offsite/local archive. Rebuild a **new** host/cluster, restore external
secrets from separate escrow, initialize the owner/runtime roles with the
controlled migrator, recover the **independent erasure journal** from separate
escrow, create a new empty `papela_restore_*` database and execute the same
restore command. Keep API/worker stopped until a designated operator validates
the journal, reviews counts/ownership and replays all erasures into the target.
Then change the external database-URL secret to the new database (do not print
it), recreate API/worker, verify readiness, cross-tenant access and a synthetic extraction, and
only then reopen traffic. If any check fails, leave traffic closed and keep
the original DB/archive intact. The script has no production-DB overwrite
override. Never use `down --volumes` on a real installation.

## Offsite design and recovery objectives

A backup only on the same VPS is **not** disaster recovery. Before real
customer data, add a second copy to a private S3-compatible bucket or another
independent failure domain. Use a service identity limited to write/read the
backup prefix, private bucket access, versioning/retention controls, TLS and
encryption (prefer client-side age recipient encryption with the recovery
identity held in separate escrow, or managed KMS encryption with independently
tested key recovery). Upload only a verified completed backup, verify remote
checksum, and periodically restore from the downloaded offsite copy into an
isolated environment. No bucket, credential, age key, KMS key, lifecycle or
cloud account is configured by this gate; do not copy plaintext archives
off-host meanwhile.

Initial **technical targets, not commercial SLAs**: daily successful backup
gives an RPO objective of at most 24 hours for completed database records
while the host/local archive survives. In-flight PDFs are excluded and require
resubmission. Host loss has no bounded RPO until offsite is implemented. A
small-MVP RTO objective is two hours **after** infrastructure, matching secrets
and archive are available; this must be recalibrated with actual database size
and an offsite drill. The synthetic local backup took ~0.34 s and the
database-only restore ~0.29 s; those tiny fixtures do not predict production
time, DNS/certificate recovery or human approval.

## Monitoring and troubleshooting

The one-shot tool emits safe JSON events: `backup_started`,
`backup_succeeded`, `backup_failed`, `restore_test_started`,
`restore_test_succeeded`, `restore_test_failed`, plus verification/retention
events. Record the job exit code and the emitted backup ID in the scheduler,
not the database password or raw `pg_dump`/`pg_restore` output. Before
customer traffic, wire alerts for a missing daily success, any failed run,
failed restore drill, fewer than two independently stored verified copies,
and low backup/database disk space. A checksum/TOC error means quarantine the
archive and use another verified copy. A restore error must be reproduced in
the isolated target; never enable verbose SQL logging against customer data.

For this gate, local scans and CI must remain zero CRITICAL/HIGH and zero
secret findings for all runtime images, including the new backup image.
The operator still needs real host permissions, offsite/key recovery,
capacity measurement and periodic restore drills before go-live.

## Local synthetic evidence for this candidate

On 2026-10-02 UTC, a fresh isolated Compose project created two tenants'
completed jobs and one pending job. The final custom-format archive was 4,979
bytes with SHA-256
`e7eeb4bad7376ad35857a39f0ef0fa70f3e29bd414a3b8dd122d3643d6bf0660`.
`verify` and `pg_restore --list` passed; an intentionally corrupted copy was
rejected with `BACKUP_CHECKSUM_MISMATCH`. A restore request targeting `papela`
was rejected with `RESTORE_TARGET_FORBIDDEN`. A disposable target database
containing a sentinel table was explicitly dropped, recreated empty and
restored in ~0.29 s. The restored database had two distinct tenants, two
completed jobs and one failed/reconciled in-flight job, the expected table
owner, trigger and runtime grants. After repointing API/worker, TLS,
health/readiness, cross-tenant 404s, both saved results, the failed job's
resubmission status and a newly processed job all passed. Retention with an
explicit test limit of one removed only the older synthetic backup after the
newer one had been verified. The source database remained intact.

All five locally scanned runtime images had zero CRITICAL/HIGH and zero secret
findings. Residual application findings: 16 MEDIUM/8 LOW; Caddy: 5 MEDIUM/4
LOW/1 UNKNOWN; backup, migrator and Postgres: zero. This evidence is not a
substitute for a successful GitHub Actions run of the final commit.

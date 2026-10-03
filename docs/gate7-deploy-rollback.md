# Gate 7 — controlled release and rollback (local/CI candidate)

This is **not** authorization to deploy to a VPS, publish a container image,
change DNS, open a firewall, or accept customer data. Gates 1–6 remain required.
The current workflow builds/scans on GitHub Actions and performs a localhost-only
rollback drill. There is no registry, release scheduler or live host configured.

## Release identity and trust boundary

The application image is tagged `papelaai:sha-<40-character Git commit>` and
has the OCI `org.opencontainers.image.revision` label. `ops/release.py record`
checks that label, runs the installed-extractor provenance check, scans the
**exact local image** for CRITICAL/HIGH vulnerabilities and secrets, and writes
`releases/<commit>.json` in a restricted state directory **outside Git**. The
manifest holds UTC creation/promotion times, Git commit, image tag, Docker
content-addressed **image ID**, nullable registry manifest digest, private
extractor commit from `uv.lock`, schema fingerprint, scan verdict, migration
status/review hash, backup ID, previous release and deploy status. It contains
no key, password, tenant mapping or document data. The image ID is a Docker
config digest; it is **not** a registry manifest digest. When a private registry
is approved, the pipeline must capture the pushed manifest digest and deploy
`repository@sha256:<digest>` instead of trusting a mutable tag. No image is
published by Gate 7.

The complete-release artifact consists of the manifest, reviewed commit,
locally pinned image bytes, `uv.lock`, schema files and scan evidence. Docker
tags alone are mutable, so the controller verifies tag → image ID before any
change and verifies the running API/worker container image IDs afterward. Do
not use `latest`, a branch name, or an ad-hoc build on the VPS as a release.
Retain at least the active and previous images plus their state records. State
files are atomically replaced, not encrypted; restrict and back them up.

## Safe sequence

```text
build exact Git commit + locked extractor
  → verify provenance, full CI, five image scans
  → record target manifest and previous active image
  → preflight (CI green, scan, secrets, disk, current health, schema policy)
  → create + verify recent Postgres backup, retain its ID
  → one-shot migration only when reviewed/backward-compatible
  → recreate API/worker/Caddy with pinned image
  → wait for Docker health and HTTPS /readiness
  → synthetic upload, extraction, tenant boundary
  → update external image setting and active manifest
  → on failure, restore previous application image only if old app supports schema
```

`ops/release.py` holds a nonblocking OS file lock during each preflight,
deploy, record and rollback (`flock` on Linux, byte-range lock on Windows).
A second command fails with `DEPLOY_LOCK_BUSY`; a crashed process releases the
kernel lock. `--execute` is mandatory for mutations. Use one state directory
per installation. The dedicated one-shot migrator already has a Postgres
advisory lock and a single transaction; API/worker never apply migrations.

Preflight fails closed unless the exact target image/label/manifest agree, its
scan recorded zero CRITICAL/HIGH/secrets, the CI push run for that SHA on
`master` succeeded, four Compose secret files and a dedicated canary key file
exist, Linux secret files are not group/world-readable, the backup filesystem
has at least 512 MiB free, Compose config parses, the current release is
registered and running with healthy API/worker and HTTPS readiness, and a
backup ID from the last 26 hours passes Gate 6 checksum/archive verification.
The 26-hour window tolerates two hours of scheduler jitter around a daily
backup; it does not weaken the 24-hour RPO objective. An initial deployment
must explicitly pass `--initial` and have no existing API/worker containers.
The CI-green check is bypassed **only** with `--synthetic`, which is restricted
to a `papela-gate7-*`/known CI Compose project and `https://localhost:<port>`.
Never use that flag for production. Network or GitHub API failure blocks a
real preflight; never substitute a claim of green CI.

### Migrations and rollback boundary

The fingerprint includes `0000_lock.sql`, `0001_initial.sql`,
`0002_tenant_isolation.sql`, and `010_runtime_role.sql` in fixed order. A
same-fingerprint application upgrade **does not rerun migrations**. A first
deployment needs explicit `--migration-class backward-compatible` after human
review of the current schema. A changed fingerprint needs an external,
reviewed JSON record with the exact `from_schema`, `to_schema`,
`classification: "backward-compatible"`, `old_app_compatible: true` and a
nonempty `reviewed_by`. The controller stores its SHA-256. It will not infer
compatibility from SQL syntax. Destructive/incompatible changes are **blocked**
by the normal deploy path even with a backup. They require a separate approved
maintenance window, a restored-backup drill and a coordinated schema/data
recovery plan; do not supply a false review to bypass this guard.

Application rollback never reverses database schema. Automatic or manual
rollback refuses a schema change unless the reviewed release explicitly
asserts that the previous app is compatible. For incompatible schema, stop
writers, preserve the failed host, and follow the Gate 6 new-target restore
procedure with a matching document-volume/secrets plan. Never restore over
`papela`, delete a volume or modify historical migration SQL in place.

### Operator commands (future approved host)

These examples are *procedural*, not a request to run them now. The external
`/secure/.env.production` contains paths/settings, never secret values. Its
`PAPELA_APP_IMAGE` must match the active manifest; the controller updates only
this setting after validated promotion/rollback. The state directory must be
restricted (`0700`), outside the checkout, and survive host restarts. The
dedicated canary tenant/key must already be in the tenant-key mapping; keep
its key in a separate `0400` file. Canary uploads create synthetic jobs that
are subject to the pending Gate 8 retention policy.

```sh
commit=$(git rev-parse HEAD)
test "$(git status --porcelain)" = ''
# On an approved build agent with the exclusive read-only extractor identity:
DOCKER_BUILDKIT=1 docker build --no-cache --ssh default \
  --build-arg "RELEASE_COMMIT=$commit" --tag "papelaai:sha-$commit" .
python -m ops.release record --state-dir /srv/papela/releases --commit "$commit"

# Start Postgres only on a fresh installation, before an initial backup.
# Existing installations must keep their current services healthy.
docker compose --env-file /secure/.env.production -f docker-compose.prod.yml \
  --profile ops run --rm backup backup
# Record the emitted BACKUP_ID without copying its secret archive to Git.
```

Use the same options for `preflight` and `deploy`; no shell variable should
hold a secret value. Example for a **normal** rollout after receiving a fresh
backup ID:

```sh
common=(--state-dir /srv/papela/releases --env-file /secure/.env.production)
python -m ops.release preflight "${common[@]}" --commit "$commit" \
  --backup-id "$backup_id" --smoke-url https://papela.example.invalid \
  --smoke-key-file /secure/canary.key
python -m ops.release deploy "${common[@]}" --commit "$commit" \
  --backup-id "$backup_id" --smoke-url https://papela.example.invalid \
  --smoke-key-file /secure/canary.key --execute
python -m ops.release status --state-dir /srv/papela/releases
```

For a reviewed **initial** deployment, add `--initial --migration-class
backward-compatible` to both calls, and first start a new empty Postgres
container. The backup must still be created/verified before migration. For a
reviewed schema change, supply `--migration-review /secure/schema-review.json`
to both calls. The controller rejects changed schema without it.

For application rollback, keep the latest backup ID and run:

```sh
python -m ops.release rollback "${common[@]}" --backup-id "$backup_id" \
  --smoke-url https://papela.example.invalid \
  --smoke-key-file /secure/canary.key --execute
```

The controller restores the previous image, waits for readiness, runs a new
extraction canary, then atomically updates the active pointer and external
image setting. If this fails, `rollback_failed` is emitted and an operator
must stop promotion and investigate; no database restore is attempted
automatically. A crash between external env-file and active-pointer writes
can leave a detectable `ACTIVE_ENV_IMAGE_DRIFT`: inspect running image IDs,
the release manifests and the real schema, then reconcile under change
control. Do not manually overwrite the active pointer by guesswork.

## Logs, checks, and incident response

Safe JSON events: `deploy_started`, `deploy_succeeded`, `deploy_failed`,
`rollback_started`, `rollback_succeeded`, `rollback_failed`,
`migration_started`, `migration_succeeded`, `migration_failed`. They carry
release ID, bounded error code, backup ID or image ID, not subprocess stderr,
secret values, PDFs or tenant IDs. Check `docker compose ... ps`, controlled
`logs --tail 100 api worker postgres caddy migrate backup`, `/health`,
`/readiness`, the release manifest and `docker image inspect`/container `.Image`.
Review logs for PII before sharing. On health failure, check database, PDF
volume/quota and worker heartbeat; on extraction failure, retain the failed
release image and safe event log. On failed migration, leave the old app in
place and test against an isolated restored backup. Never run `down --volumes`
on production.

The Gate 7 drill uses a separate localhost-only Compose project with a new
Postgres volume and synthetic tenant keys. It performs A → B → automatic A
after injected post-deploy failure, then B → manual A, checks both tenants'
existing jobs, a new job, image IDs, health/readiness and persistence across
service restarts. It does **not** test an incompatible live migration (that
path is blocked), production ACME/DNS, registry digest publication, off-host
recovery, customer data, load, or zero-downtime replicas. Those remain
explicit go-live blockers/risks, not implied passes.

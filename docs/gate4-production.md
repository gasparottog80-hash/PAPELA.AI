# Gate 4 — single-VPS production infrastructure

This is an infrastructure *candidate*, not a go-live authorization. The
local Gate 4 revalidation has passed; final status also requires a successful
GitHub Actions run for the exact pushed commit. The original Caddy and pypdf
HIGH findings were remediated and all four final runtime images scanned at
zero CRITICAL/HIGH. See [the Caddy remediation evidence](gate4-caddy-remediation.md).
Do not expose this stack to the public Internet. No VPS,
DNS, firewall, public certificate, production secret or customer data was
created or changed during this gate. The isolated local test uses synthetic
tenants and a Caddy local CA. A real deployment requires the remaining gates,
including a tested off-host backup/restore (Gate 6), retention decisions and a
separate operator approval.

## Topology and trust boundaries

```text
Internet --HTTPS :443--> Caddy --private edge network--> API
                        :80 redirects                |
                                                     | database network
                              worker ----------------+--> Postgres (pgdata)
                                |                    |
                                +-- private pdfs ----+

Operator only: one-shot migrator --database network--> Postgres
```

Only Caddy publishes host ports 80/443. API, worker, Postgres and migrator
have no published ports. `database` is an internal Docker network. API and
worker share the private `pdfs` named volume; only API receives tenant keys.
The runtime database login is a dedicated `papela_runtime` role with DML on
`jobs`, no schema CREATE, no superuser/role/database creation and no table
ownership. The owner password exists only in Postgres and the one-shot
migrator, never in API or worker. API and worker use the same privately built,
locked application image; the private extractor remains installed from the
commit pinned in `uv.lock`. Neither source nor an SSH key is copied into this
public repository or published as an image artifact.

`docker-compose.prod.yml` is intentionally separate from the development
Compose file. Do not combine the two. Builder/base images are digest-pinned;
application and Caddy images are built locally and never implicitly pulled.
A final registry digest would require separate private publication/change
control. The Postgres service derives from a pinned official image but starts
as UID 70 and removes the unused, vulnerable `gosu` privilege-switch binary.
It expects a fresh Docker named volume initialized with that ownership, not
an arbitrary pre-existing host bind mount. Caddy stores certificates in
`caddy_data`, Postgres data in `pgdata`,
and private documents in `pdfs`. Back up all necessary volumes/keys under an
approved procedure; the local restart test does **not** constitute backup.

## Provisioning prerequisites (future operator action)

1. Size and harden one VPS for the actual measured document load (the Compose
   limits alone do not establish capacity). Put a real FQDN A/AAAA record on
   that host; open only SSH from an approved source and TCP 80/443. Do not
   publish 5432 or 8000. Public Caddy certificate issuance needs working DNS
   and inbound 80/443; this gate has tested only local TLS.
2. Install a maintained Docker Engine/Compose, enable host patching, disk
   monitoring, restricted operator accounts and encrypted storage as required
   by policy. Use a dedicated read-only extractor SSH identity in the build
   agent, not a developer's personal key. `docker build --ssh default` forwards
   the agent into one BuildKit step; the image must remain private.
3. Copy `.env.production.example` to an untracked environment file **outside
   the repository** and replace every placeholder. Place the four secret
   files outside the Git checkout under a root-owned `0700` directory. On
   Linux with file-backed Compose secrets, host UID/mode are bind-mounted:
   make admin/runtime password files owned by UID 70 (`0400`) for the
   non-root migrator, and API DB URL/tenant-key files owned by UID 65532
   (`0400`) for API/worker. The UID-70 Postgres entrypoint reads its admin
   file. Verify these permissions on the target host; Compose's
   `uid`/`mode` options do not remap file-backed secrets. Generate independent
   random admin/runtime passwords of at least 24 characters; put the runtime
   password in the runtime DB URL.
   Use unique high-entropy tenant keys mapped to immutable tenant UUIDs. The
   database URL and key mapping are file *contents*, never Compose env values
   or command arguments. Never paste real secret values into CI logs or issues.
   Docker Compose file secrets are mounts, not encryption at rest; host root
   access and backups still need protection.
4. Choose and record a data retention and backup/restore policy before real
   customer intake. Postgres extracted JSON is retained until an authorized
   DELETE; raw PDFs are purged after successful processing and the periodic
   sweep handles leftovers. Gate 8 must settle legal retention/exclusion.

## Controlled startup and migration

Use explicit `--env-file /path/outside/repo/.env.production` on **every**
Compose command, with `-f docker-compose.prod.yml`. First validate
`config --quiet` and verify the four secret-file paths, the intended domain,
port bindings, image ID and available disk. Build the application image from
the current reviewed commit with the dedicated read-only BuildKit SSH agent;
run `python scripts/verify_extractor.py` inside it and check the pinned commit.
Build the Caddy/migrator/Postgres images locally from their pinned bases:

```text
docker build --ssh default --tag papelaai:gate4 .
docker run --rm --network none papelaai:gate4 python scripts/verify_extractor.py
docker compose --env-file /path/outside/repo/.env.production -f docker-compose.prod.yml build postgres migrate caddy
```

These commands are a future operator runbook, **not** permission to deploy the
local candidate. Before a VPS is eligible, scan its actual final images and
rerun the complete smoke/CI checks.

Take and **restore-test** a database backup before *any* migration on a
nonempty database. Gate 6 has not supplied this procedure, so an existing
production database must not be migrated yet. For a new empty database, start
Postgres and wait for health; then run the migrator explicitly:

```text
docker compose --env-file /path/outside/repo/.env.production -f docker-compose.prod.yml up -d --wait postgres
docker compose --env-file /path/outside/repo/.env.production -f docker-compose.prod.yml --profile ops run --rm migrate
docker compose --env-file /path/outside/repo/.env.production -f docker-compose.prod.yml up -d --wait api worker caddy
```

The migrator executes `0001_initial.sql`, the Gate 3 tenant quarantine and
immutable-owner migration, and runtime-role grants in one transaction under
an advisory lock. It is not an API/worker entrypoint and does not run on
restart. Re-running it on the isolated empty fixture was idempotent. If a
migration fails, it rolls back its transaction and the application remains
unready; investigate without editing the database by hand. Historical
shared-trust jobs are quarantined and cannot be assigned to customers by
guesswork.

`/health` means the API process is alive. `/readiness` is 200 only when its
Postgres connection succeeds and the private PDF directory is writable with
headroom for another upload; otherwise it is 503. Caddy actively checks
`/readiness` with the configured domain Host. The worker has a loop heartbeat
probe and restart policy. An empty `/health` success does not prove the worker
is processing jobs; the synthetic upload/poll flow is the release smoke.

## Local verification and recovery

The local test binds Caddy only to `127.0.0.1:18080/18443`, uses a local CA
and synthetic keys, and starts a separate project named `papela-gate4-local`.
It checks HTTPS certificate verification, security headers, health/readiness,
unauthenticated rejection, PDF extraction, cross-tenant 404s, database and
worker restart persistence, and owner deletion. CI repeats that topology on
the exact pushed commit with newly generated synthetic database passwords.
The test also runs the migrator twice to catch non-idempotent changes.

For normal operation, inspect `docker compose ... ps` and `docker compose ...
logs --tail 100 api worker postgres caddy` (do not upload raw logs without a
PII review). A 503 from Caddy points first to API `/readiness`, database
reachability and PDF-volume free space. A worker health failure points to a
stalled loop or a missing `/tmp` heartbeat. A failed migration leaves API
startup blocked; inspect the migrator's exit code and review SQL against a
restored test backup. To stop without deleting data, use `docker compose ...
stop`; never use `down --volumes` on production. Caddy's runtime logs discard
request headers to avoid recording `X-API-Key` even on proxy failures.

For a bad application-only release, retain the known-good local image,
repoint `PAPELA_APP_IMAGE` to that immutable reviewed image and recreate API
and worker. Do not run `down --volumes`, rewrite history, or delete customer
records. If the schema changed incompatibly, stop writers and restore the
pre-migration database backup using the Gate 6 procedure before restarting
the previous image. Restoring Postgres without its associated document volume
and secrets can yield inconsistent or unreadable jobs; the recovery plan must
cover that set. Caddy certificate volume should also be preserved. None of
these real restore actions was attempted in Gate 4.

## Security revalidation and remaining production blockers

- The original official Caddy binary had **17 HIGH findings**. The custom
  rebuild of the same official Caddy 2.11.4 source with patched Go modules
  scanned at **zero CRITICAL/HIGH** and passed the local TLS/topology smoke;
  [the separate evidence](gate4-caddy-remediation.md) records every advisory,
  source checksum, image ID and residual MEDIUM/LOW items. It is not a
  released or deployed artifact solely because of this local test.
- The pre-update application image had three HIGH pypdf 6.18.1 advisories:
  `CVE-2026-102998` (form-field denial of service), `CVE-2026-102999`
  (embedded-file denial of service) and `CVE-2026-103000` (page-label denial
  of service). Trivy reported 6.19.0 as the fixed version for each. The
  minimum requirement is now `pypdf>=6.19.0` and `uv.lock` pins 6.19.0;
  no other locked package version changed. The fresh application image
  confirmed 6.19.0 at runtime and retained the proprietary extractor at
  `ddb485ff76627f2e995b11d2b4d11325fc5628c9`. Its all-severity scan
  found **0 CRITICAL, 0 HIGH, 16 MEDIUM, 8 LOW, 0 secrets**. The MEDIUM
  records are 13 in `libc6`, two in `zlib1g` and one in `libgcc-s1`;
  the eight LOW records are in `libc6`. The scanner reported no fixed
  package version for these residual Debian findings. Caddy had 5 MEDIUM,
  4 LOW and 1 UNKNOWN; Postgres and migrator had no vulnerability or secret
  findings in this scan. Track and reassess these residuals against later
  vulnerability DB and base-image updates; no waiver for a future HIGH.
- The fresh, localhost-only synthetic stack passed two migration runs,
  HTTPS certificate validation, `/health`, `/readiness`, extraction, tenant
  isolation, a 23 MiB upload rejection (HTTP 413), and persistence of jobs
  and the Caddy local CA after restarting Postgres, API, worker and Caddy.
  All four services were healthy. The 79 tests passed with zero skips. These
  are local findings; the exact-commit CI run remains a separate release gate.
- No real DNS/ACME issuance, VPS hardening, external firewall or load test.
- Host hardening is an operator prerequisite: SSH key authentication, restrict
  administrative SSH source IPs, disable password authentication after access
  is verified, disable/restrict root SSH login, patch the OS and consider
  fail2ban for exposed SSH. Do not enable a firewall remotely without a
  tested recovery path.
- No tested off-host backup and restore yet (Gate 6).
- One VPS is a single point of failure; Compose limits are not capacity proof.
- One API replica uses an in-process rate limiter; horizontal scaling needs a
  shared limiter. The current worker is single-instance by design.
- Production supports selectable-text PDFs only. Scanned/image-only documents
  remain unsupported until the native OCR advisories and isolation are solved.
- Extracted JSON retention/erasure policy and full LGPD review remain open.

Do not mark the entire product ready for sale or production traffic solely
because this local infrastructure test and CI pass.

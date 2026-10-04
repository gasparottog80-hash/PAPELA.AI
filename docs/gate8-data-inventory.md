# Gate 8 — data lifecycle inventory and technical controls

This is a technical inventory for the Gate 8 candidate based on
`f3651b9e5c82e6dec7d0b096a6e947b0cf508d5d`, not a privacy notice, legal
opinion, or claim of LGPD compliance. It authorizes no live deletion, restore,
deployment or customer intake. Gates 3, 5 and 6 remain the security,
observability and backup baselines.

## Current data inventory

“Sensitive?” below means *potential privacy/security impact*, not a legal
determination that a field falls in the LGPD's special-category definition.
The tenant boundary is the immutable UUID on `jobs`; there is no tenant table.

| Data | Origin and technical purpose | Location and persistence | Tenant | Sensitive? | Current retention / deletion | In Gate 6 backup? |
| --- | --- | --- | --- | --- | --- | --- |
| Uploaded PDF | Customer upload for parsing/extraction | Private `/data/pdfs` volume while queued/processing; filename on disk is server UUID | `jobs.tenant_id` | Yes; may contain personal/fiscal data | Production deletes after successful processing or terminal failure. Worker sweeps failed jobs and completed orphan PDFs; `PAPELA_RETENTION_DAYS` defaults to 7 for non-purged completed files. Owner deletion removes a terminal job's file. | No |
| Full extracted text and table content | Local PDF text/OCR processing for extraction and API result | `jobs.result` JSONB (`pages[].text`, table rows/HTML where applicable) | Job owner | Yes; can contain CPF/CNPJ, names and other personal data | Terminal jobs expire after configurable `PAPELA_JOB_RETENTION_DAYS` (technical default 30); owner can delete earlier. | Yes |
| Fiscal fields, including CPF/CNPJ | Proprietary extractor output for customer result | `jobs.result.fields` JSONB | Job owner | Yes; CPF is personal data, CNPJ may identify natural persons in context | Same terminal-job expiry and owner deletion. | Yes |
| Job metadata | Server UUID, status, original display filename, path, size, page count, attempts, timestamps, sanitized error | `jobs` table | Immutable `tenant_id` | Filename/path may expose information | Terminal-row expiry; owner deletion; offboarding removes all tenant rows. Active owner deletion returns 409. | Yes |
| Tenant UUID | Operator-supplied key mapping, job owner and erasure marker | External tenant-key secret, `jobs.tenant_id`, independent journal | Self | Pseudonymous identifier | Offboarding deletes active rows; minimal marker remains to protect old backups. | In job rows only |
| API keys | Operator provisions tenant-to-key JSON; authenticate customer requests | External Compose secret file mounted into API, loaded in memory; not written to jobs | One key per tenant | Credential | Replacement requires API recreate. Rotation/revocation records a key-free journal/audit event. Revocation blocks tenant even before secret replacement. | No |
| Application/worker logs | Health, request/job/error/audit diagnosis | Docker `json-file` stdout/stderr | No tenant ID; job/request UUIDs may correlate | Pseudonymous operational data | `PAPELA_LOG_MAX_SIZE`/`PAPELA_LOG_MAX_FILES` bound size, **not age**. Manual host purge not documented as an executable time policy. | No |
| Caddy/Postgres/migrator/backup logs | Proxy, DB and operations diagnosis | Docker stdout/stderr, size-rotated | No tenant label by design | Could become sensitive if configuration regresses | Size-bound only. Caddy strips URI/query, IP, headers; Postgres suppresses statement/parameter logging. | No |
| Metrics | Aggregate operation/latency/health diagnosis | Process memory, loopback-only HTTP; reset on restart | No tenant/job labels | Low if allowlists remain enforced | Ephemeral; no durable retention or collector configured. | No |
| Postgres backups | Recovery of completed results and metadata | Restricted external backup directory, custom `pg_dump` archives | All tenants in one archive | Yes; plaintext fiscal results | Verified-copy count plus optional age command (`PAPELA_BACKUP_RETENTION_DAYS`, technical default 30), preserving newest two verified copies. No scheduler yet. | This is the backup |
| Erasure journal and backup inventory | Prevent deleted records reviving after restore; track recoverable snapshot horizons | Restricted independent bind mount, outside Postgres snapshots | Minimal tenant/job UUIDs in journal; backup IDs/locations in inventory | Pseudonymous marker and operational metadata | No automatic deletion. Test-only compaction of job markers requires verified backup destruction and preserves a restore floor. Independent backup required. | No |
| Release manifests | Provenance, image/schema/backup identity and rollback | External restricted release state directory | No tenant field | Operational metadata | No explicit expiry; keep active and previous releases. | No |
| External credentials / Caddy ACME keys | DB access, API auth, TLS | Separate host secret files and Caddy data volume | Tenant mapping contains UUID/key | Yes | Operator-controlled rotation and escrow, outside ordinary Postgres dump. | No |

## Technical guarantees observed, and gaps

- Upload admission bounds bytes/pages, uses a server-generated storage path, and
  removes rejected partial files. The worker persists a result before deleting
  its source PDF; the sweep handles a crash between those operations.
- Every customer-visible job lookup and terminal deletion is scoped by the
  authenticated tenant UUID. Cross-tenant and nonexistent IDs both return 404.
  The API has no cookie session, so browser CSRF is not its current threat path.
- Allowlisted JSON logs omit raw document, OCR text, Authorization, API key,
  request payload and tenant UUID. Docker-level Caddy/Postgres filtering must
  remain part of every regression test. Job/request UUIDs are still linkable
  pseudonyms and need access control.
- The worker now purges expired terminal rows/results through a durable marker;
  customer deletion and operator offboarding/export are tenant-scoped. The
  upload path serializes insertion with offboarding via the journal lock.
- An isolated restore sets `privacy_state.restore_ready=FALSE`. API and worker
  deny traffic until every journal marker has been validated and replayed.
  A missing/corrupt journal or generation mismatch fails readiness closed.
- **Remaining:** age-based Docker log deletion is not controlled by Compose's
  `json-file` driver (only size/count are bounded). Journal compaction is
  test-only; production lacks independently attested inventory, deletion
  receipts and offsite coverage. No production schedule/offsite copy is installed.
- Current `PAPELA_TENANT_API_KEYS` is plaintext **inside an external secret
  file**, not in Git or the database. Docker/host administrators who can read
  that file can read the keys. A hash-only IAM store is not present.

## Implementation boundary

`app/erasure.py` stores schema-checked events with an atomic, checksummed index,
fsyncs and restrictive permissions. SHA-256 detects accidental corruption, **not
malicious tampering**; access controls and independent backups remain essential.
Each marker is durable before PDF/row deletion. Offboarding blocks new uploads
and worker result persistence; a quiesced API/worker is still required during
restore reconciliation. The journal must outlive all backups that could carry
the deleted data. See [Gate 8 operations](gate8-data-lifecycle.md).

The operator-only export produces an exclusive `0600` JSON file containing
only one tenant's job/result records, without API keys, internal storage paths
or console payload. Authorization would be via a restricted operator role and
an exact tenant confirmation; customer self-service export is a separate
product decision. The CLI requires an explicit tenant, exact confirmation,
`--execute` and a separate enablement environment variable.

## Retention policy decisions still required

| Class | Existing technical behavior | Decision needed before go-live |
| --- | --- | --- |
| Transient PDFs | Immediate production purge after done/terminal failure; seven-day fallback for orphan completed files | Confirm maximum recovery window for stalled/failed uploads and incident handling |
| Jobs/results, text and fiscal fields | Configurable terminal-row expiry, default 30 days, hourly worker sweep | Contract/purpose-based period, legal preservation exceptions and evidence model |
| Logs | Rotated by bytes/count only | Calendar retention, host purge procedure, access controls, incident hold |
| Backups | Verified-copy count plus operator-invoked age purge, default 30 days, keeping newest two | Maximum age, independent encrypted copy, scheduler, legal hold and restore reconciliation |
| Metrics | In memory until restart | Future collector retention if introduced; avoid identifier labels |
| Tenant IDs and audit markers | Offboarding/credential markers retained; test-only job-marker compaction behind backup proof | Offboarding approval, protected journal/inventory backups, independently audited inventory and deletion proof |
| Release manifests | Keep active/previous; no calendar period | Operational record period and secure disposal |

The controller/business owner and qualified legal counsel must decide the
processing roles, purposes, lawful basis, retention and exception periods,
handling of data-subject requests, subprocessors/hosting location, contract
terms and public privacy notice. Technical defaults are not legal approval.
The [LGPD text](https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13709.htm)
and [ANPD guidance on data-subject rights](https://www.gov.br/anpd/pt-br/assuntos/titular-de-dados-1/direito-dos-titulares)
should be reviewed with counsel, particularly termination, preservation and
deletion exceptions. The [ANPD FAQ](https://www.gov.br/anpd/pt-br/acesso-a-informacao/perguntas-frequentes)
also explains that deletion requests require case-specific analysis.

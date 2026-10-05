# Production enablement — local design, not go-live approval

Gate 9 remains **BLOCKED**. This record covers only code, local tests and
future operator steps. No VPS, bucket, DNS, credential, GHCR package or
customer pilot was created. The decision gates in
`docs/gate9-go-live-readiness.md` still apply.

## Immutable private runtime

The proposed registry is private
`ghcr.io/gasparottog80-hash/papela-ai`. The manual-only
`.github/workflows/publish-private.yml` has job permissions `contents: read`
and `packages: write`; `GITHUB_TOKEN` is sufficient to publish a package
linked to this repository according to GitHub's Container registry guidance.
It requires an exact green master-push baseline run, the exclusive read-only
extractor Deploy Key, locked fresh build and zero-CRITICAL/HIGH/secret scan.
It tags only `sha-<full 40-character Git SHA>`, never `latest`. It captures
the **registry manifest digest returned by push**, pulls `repository@digest`
and verifies the revision label and local image ID. GitHub says a newly
published package is private by default, but a named owner must inspect
package visibility/access before authorizing the first push and immediately
afterward. The workflow has not been dispatched. A push is a separate,
explicitly authorized external action; do not expose the image as an artifact.

`ops/release.py record --registry-digest sha256:<64 lowercase hex>` may be
used only after an approved pull by that digest. The manifest records commit,
local image ID (config digest), registry repository/tag/**manifest digest**,
build timestamp, extractor commit/version, schema fingerprint/version, scan
verdict, migration compatibility, backup reference and previous release
digest. Production preflight refuses missing/malformed digest, wrong repo/tag,
missing pulled digest, image/provenance mismatch, unscanned image and drift.
Production Compose uses `repository@digest`; rollback uses the previous
manifest's digest. Local image ID is never substituted for a registry digest.
Retain active and previous digests and pull permissions throughout a rollback
window. Protect `releases/` and `.env.production` outside Git; do not edit a
manifest by hand. No real registry digest exists yet.

## Offsite backup contract and inventory

Candidate: private S3-compatible bucket in a separate provider/region and
failure domain, with a dedicated **prefix-scoped write identity** and a
separate read/recovery identity. Provider, bucket, location, retention and
contractual/legal period are **not selected**. The intended sequence is:

1. Produce and locally verify the Gate 6 archive, metadata, checksum and
   `pg_restore` TOC; snapshot the independent erasure journal/index and
   inventory at a consistent privacy generation.
2. Encrypt archive, journal and inventory **before** off-host transfer using
   a separately escrowed recipient/key; never put recovery material in Git,
   the image, workflow logs or the upload identity.
3. Upload into an immutable/versioned prefix with transport encryption and
   least privilege. Capture object version, remote checksum and listing/HEAD
   verification; record a receipt without credentials or document content.
4. Re-download using the recovery identity to an isolated restore target;
   verify decrypted checksum, restore database and journal, reconcile erasures
   and enforce restore floor. Exercise at least weekly until a formal RPO/RTO
   and schedule are approved. Alert on missed daily copy (>26h), verify
   failure or missed restore drill.
5. Expiry is not deletion. Legal hold, object-lock policy and physical deletion
   need separate operator evidence. Never delete a journal generation still
   required by any restorable database copy.

`ops/offsite.py` supplies a transport contract and a **test-only plaintext
mock** that refuses non-test environments. It does not implement encryption,
S3 access or real credentials. The Gate 8 inventory distinguishes local vs
offsite, present/verified metadata, missing, expired, deleted and legal hold;
unknown, missing, invalid and every offsite entry still block journal
compaction. SHA-256 detects accidental corruption, not malicious rewriting.
There is no independent provider receipt verifier or mounted least-privilege
inventory view yet. Consequently, production offsite/recovery and mutating
compaction remain blocked. Never set `PAPELA_ENV=test` on a real host.

## Logs and operational alerts

The base Compose retains Docker's size/count-only `json-file` rotation for
local and CI drills. Production release commands additionally load
`docker-compose.prod.journald.yml` (Compose >=2.24.4), replacing those options
with the journald driver on every service. On a Linux/systemd host, render
`python -m ops.journald_policy --days 14 --max-use 1G --keep-free 5G` and
have an operator review then install it as a systemd journal drop-in. The
renderer **does not install or restart anything**. This is a host-wide policy:
budget all host/container writers, verify `docker logs`, journal access and
actual vacuum behavior after deployment. Default 14 days is technical, not a
legal retention period. Set the final period only after legal/privacy review.
Restrict journal reading to authorized operators; export/redact safe events,
not raw PDFs, tenant keys or SQL values. An incident hold requires a separately
encrypted, access-controlled export before ordinary expiry, with owner,
reason and expiry recorded. Never truncate Docker-owned log files or bypass
the age policy by silently switching drivers.

| Alert | Trigger candidate | Check / escalation |
| --- | --- | --- |
| API unavailable | External HTTPS probe fails 2 consecutive minutes | Page on-call; test from another network |
| Readiness false | `/readiness` non-200 for 2 minutes | Inspect DB, volume, worker and active digest |
| Worker stale | Heartbeat age >3 minutes | Check queue and restart only after preserving evidence |
| Backup stale | No verified local backup in 26 hours | Stop releases; investigate scheduler and restore |
| Offsite failed/stale | Upload/verification failure or no receipt in 26 hours | Stop deletion/compaction; page recovery owner |
| Disk/inodes | 70% warn, 80% page, 90% deny intake | Check DB, PDF quota, backups, journal, logs |
| Postgres unavailable | DB health fails 2 checks | Page; preserve data, avoid blind volume recreation |
| Jobs stuck | Oldest pending/processing >15 minutes | Inspect worker and bounded retries |
| TLS expiry | Certificate expires <21 days | Page; inspect DNS/ACME state and renew path |
| High error rate | 5xx >2% over 5 minutes with >=50 requests | Correlate release/request IDs; consider rollback |

These are **proposed** thresholds, not an installed alerting system or SLA.
Assign a named on-call receiver and independently test delivery, silencing,
escalation and every firing condition on the real host. Keep labels low
cardinality and free of tenant/job/document identifiers. Same-host monitoring
alone cannot detect host loss.

## One-VPS sizing and host preparation

Use supported Ubuntu 24.04 LTS x86_64 as the initial candidate. Minimum
**planning** size is 4 vCPU/8 GiB RAM/160 GiB encrypted SSD; recommended
pilot headroom is 6–8 vCPU/16 GiB/250–320 GiB. These are estimates, not load
evidence. At >70% sustained CPU, >70% RAM, >70% disk, or backup/restore
windows that threaten RPO, resize or redesign before adding customers. A
2–4 GiB encrypted swapfile is a transient safety net, not capacity. Budget
OS/images, DB growth, PDF quota, two local verified archives, independent
journal/inventory and >=25% free disk. Prefer ext4 or XFS, UTC on host and
containers, working NTP and a tested snapshot/recovery channel. Pin a
supported Docker Engine/Compose release, keep Compose >=2.24.4, schedule
security updates and re-scan images after patching.

Host-hardening runbook — **review and execute only after a VPS is approved**:

1. Verify provider console recovery and a second administrative SSH session.
   Create a named non-root deploy/operator user with audited sudo. Docker
   socket/group grants root-equivalent control; do not grant it to untrusted
   accounts. Record the approved SSH key fingerprint and source IPs.
2. Only after a second key-only session and console recovery work, set
   `PasswordAuthentication no` and `PermitRootLogin no` in an SSH drop-in,
   validate `sshd -t`, reload and test another connection before closing the
   original. Never run an unreviewed script that could lock out the host.
3. Set default-deny inbound firewall allowing approved SSH source IPs and
   public TCP 80/443 only; check both IPv4 and IPv6 and Docker port publishing.
   TCP 5432, 8000 and metrics remain private. Verify with independent scan.
4. Install Docker Engine/Compose from the vendor's signed supported packages;
   record versions and patch cadence. Enable time sync, unattended **security**
   updates with a planned reboot window. Add fail2ban only if SSH exposure
   and monitoring justify it; it does not replace key-only access.
5. Create separately owned restricted directories for app checkout, backups,
   erasure journal, inventory, release state, secrets and Caddy ACME state.
   Directory mode 0700; secret files 0400/0600 with the UID needed by Compose
   mounts. Test ACLs and encrypted escrow before writing any real secret.
6. Install the reviewed journald policy, disk/inode monitoring and independent
   probe. Verify retention and alert delivery. Record host baseline and an
   out-of-band recovery procedure. Do not run this runbook on this machine.

## Production secrets inventory — values deliberately absent

All values live outside Git/images/logs. File mode refers to host-side mounts;
ownership must match the consuming container UID. Recovery is an independent
encrypted escrow with access audit and a practiced restore, not a copied
`.env` in the repository.

| Name | Consumer / format / source | Storage and mode | Rotation / revocation / backup / recovery |
| --- | --- | --- | --- |
| DB owner password | Postgres/migrator/backup; high-entropy random text | External owner-readable file, 0400 | Rotate with maintenance and dependent files; revoke old; encrypted escrow yes, restore test |
| DB runtime password and URL | API/worker; SCRAM password and PostgreSQL URL | External UID-65532 file, 0400 | Rotate role and recreate consumers atomically; reject old; encrypted escrow yes |
| Tenant API key map | API; UUID→unique random key JSON | External UID-65532 file, 0400 | Dual-control issue/delivery, rotate/revoke per tenant; encrypted escrow yes, tightly scoped |
| Synthetic canary key | Release smoke; dedicated high-entropy key | Separate operator file, 0400 | Rotate with canary mapping; revoke after drill; escrow optional |
| GHCR pull credential | Deploy host; package **read-only** token | Host credential store/0400 file, not CLI argv | Rotate/revoke on compromise; recover via new scoped identity, not backup of token |
| Deploy SSH key | Named operator; SSH private key | Operator hardware/secure store, never image | Revoke authorized_keys and rotate; recovery via provider console; backup per key policy |
| Offsite upload identity | Backup operator; prefix-scoped credential | External secret store/0400 | Independent rotation/revocation; replace identity, no plaintext backup |
| Offsite encryption key | Restore operators; age/KMS recipient | Separate escrow/KMS, never upload host-only secret | Rewrap/rotate with recovery test; escrow **mandatory** |
| Journal/inventory integrity key | Not implemented; no MAC exists | None | Do not invent or claim authenticity; design separately if adversarial host in scope |
| Caddy ACME state | Caddy; private account/cert keys | Restricted Caddy volume | Test renewal/reissuance; encrypted state backup or reissuance path, revoke compromised cert |
| CI extractor Deploy Key | CI only; exclusive read-only SSH v2 | Existing Actions secret, never VPS | Revoke key and secret together; regenerate, no private-key backup in repo |

## Deployment and real production drill — future, not executed

The operator must first obtain approval for VPS, package publication,
offsite provider/bucket, credentials and DNS separately. No command in this
record authorizes those actions. Once approved, use change control:

1. Preflight host baseline, named responder, clean exact Git commit and green
   push CI. Check package is private; pull scanned `repository@sha256:digest`
   with read-only identity. Compare digest to release manifest, label, image
   ID and locked extractor. Keep previous digest locally available.
2. Confirm restricted secrets, independent journal/inventory view, disk,
   real backup/offsite verified receipt and recovery key. Record baseline
   backup ID. If any copy is unknown, stop. Review migration compatibility.
3. Run one-shot migration, start services with digest-pinned Compose, verify
   health/readiness, public DNS and ACME/TLS chain. Use a synthetic tenant to
   test auth denial, isolation, bounded upload, extraction, result, source
   purge and restart persistence. No customer document.
4. Create local backup and encrypted offsite copy. Independently download and
   restore into a **new** isolated DB/host; restore journal first, reconcile
   and test restore-floor rejection of an older snapshot. Do not overwrite the
   active DB or run `down --volumes`.
5. Exercise same-schema A→B→A by **previous digest**; if schema changes,
   require a separate reviewed compatible-migration/restore drill. Fire and
   acknowledge API, worker, DB, disk, backup, offsite, job, 5xx and TLS alerts.
   Reboot/restart, verify persistence, observe disk-pressure denial behavior.
6. Save safe evidence: commit, manifest digests, scanner result, backup and
   restore receipts, schema, TLS, alert deliveries, timings and named GO/NO-GO
   signatures. Monitor after change. Roll back only when compatibility is
   proven; preserve old image/data/journal and investigate failures.

Current production preflight still cannot authenticate a real offsite receipt
or prove its mounted inventory view, and no real host/alert/TLS/pull drill has
occurred. Treat these as explicit **stop conditions**, not waivers. The first
GHCR push and real offsite configuration require a new authorization.

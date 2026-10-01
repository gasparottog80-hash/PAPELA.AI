# Gate 3 — security revalidation (2026-10-01)

Status: **local security revalidation PASS; final gate depends on real CI**.
This revision can only be marked Gate 3 PASS after its exact commit has a
successful GitHub Actions run. Gate 3 is a code/runtime security gate, **not**
a commercial go-live approval. No production deploy, VPS, Caddy, extractor
source, deploy key or GitHub secret was changed.

The earlier blocked assessment is recorded in commit
`745ece1214746cd3219dde3112088ad835166702`. This document supersedes its
shared-trust and image-risk conclusions for the changes in this revision.

## Risk closure and boundaries

| Risk | Severity before | Mitigation | Local evidence | Residual |
| --- | --- | --- | --- | --- |
| Runtime OS vulnerabilities | HIGH, 10 distinct CVEs / 50 findings | Minimal pinned distroless Debian 13 runtime; copy only needed Python, virtualenv and libraries; pin patched OpenSSL; remove affected utilities and build toolchain | Fresh Trivy 0.74.0 scan: 0 CRITICAL/HIGH, 0 secret findings; runtime filesystem check | 24 LOW/MEDIUM findings; rescan on every CI build |
| Optional native OCR | CRITICAL/HIGH advisories | No `ocr` extra or native models in production image. Production startup accepts only `PAPELA_OCR_ENGINE=text` | Package import check; dependency audit; production configuration tests | Image-only/scanned PDFs are rejected; native OCR remains unapproved |
| Cross-customer job access | HIGH | A unique API key maps to an immutable tenant UUID; SQL authorizes every customer-visible job lookup/deletion by tenant | 77 tests and E2E cross-tenant denial | No enterprise RBAC; tenant provisioning is operator-managed |
| Legacy jobs without owner | HIGH | Explicit non-destructive migration quarantines them under a reserved, API-inaccessible UUID | Migration/backfill/idempotency tests | Ownership cannot be inferred; operator must separately review any reassignment |
| Runtime/image secrets | HIGH | BuildKit SSH mount only in builder; no personal key, Git/SSH, package manager or private checkout in runtime | Trivy secret scan 0; history marker check false; filesystem check | Pattern scans cannot prove absence of every unknown secret format |

The HTTP API uses `X-API-Key`, not cookies, so browser-session CSRF is not an
applicable attack path. Auth and admission limits run before multipart parsing.
Client input is not used as a shell command, database path or outbound URL.
Postgres queries use bound parameters. The existing per-process rate limit is
appropriate only for a single API process; multi-replica coordination remains
out of scope. This gate does not certify the entire native PDF parsing stack
against unknown vulnerabilities.

## Runtime CVE triage

The starting image (`sha256:e97a5c9f0b0aa6be08beaf89468bd068106167aa67e5f37cc6852e4cf1b5923a`)
was rescanned on 2026-10-01: 204 vulnerability occurrences, 50 HIGH, 0
CRITICAL, 10 distinct HIGH CVEs, 0 secret findings. These were Debian runtime
packages inherited from the Python slim base, not merely builder packages.
The scanner's `FixedVersion` was empty for eight IDs; the two OpenSSL IDs have
the Debian fix shown below. Occurrence counts include binary-package repeats.

| CVE | Old affected package/version | Attack prerequisite and disposition |
| --- | --- | --- |
| CVE-2025-69720 | ncurses `6.5+20250216-2` | Terminal `infocmp` processing; utility absent in final runtime |
| CVE-2026-16742 | systemd `257.13-1~deb13u1` | `systemd-homed` local escalation; no systemd service in final runtime |
| CVE-2026-54369 | acl `2.3.2-2+b1` | ACL/path handling with local privilege; affected library absent in final runtime |
| CVE-2026-76642 | util-linux `2.41.5-0+deb13u1` | Privileged mount helper; affected utility absent, runtime non-root/capabilities dropped |
| CVE-2026-78408 | util-linux `2.41.5-0+deb13u1` | Privileged `nsenter --join-cgroup`; utility absent |
| CVE-2026-78409 | util-linux `2.41.5-0+deb13u1` | `mount` symlink path; utility absent |
| CVE-2026-78410 | util-linux `2.41.5-0+deb13u1` | `mount` fstab TOCTOU; utility absent |
| CVE-2026-9538 | perl `5.40.1-6+deb13u1` | `Archive::Tar` processing; Perl absent |
| CVE-2026-75804 | OpenSSL `3.5.7-1~deb13u2` | QUIC memory exhaustion; runtime copies pinned `libssl3t64=3.5.7-1~deb13u3` |
| CVE-2026-84782 | OpenSSL `3.5.7-1~deb13u2` | DTLS heap disclosure; same Debian security update |

For the first eight, no compatible Trixie fix was available in the scan; the
mitigation is **removal of the affected runtime components**, not a claim that
the vulnerable versions became safe. Debian's
[security tracker](https://security-tracker.debian.org/tracker/) contains the
individual advisories; [DSA-6531-1](https://security-tracker.debian.org/tracker/DSA-6531-1)
records the OpenSSL update. The builder can still contain build-only tooling;
it is not published or run as a service. The final image derives from pinned
digests and copies only CPython 3.11, its locked virtualenv and needed shared
libraries. `libssl3t64` is pinned to the explicit Debian security revision so
a missing package fails the build rather than silently selecting another one.

The fresh final image scan found 24 remaining findings (16 MEDIUM, 8 LOW),
**0 CRITICAL/HIGH, 0 Python advisories and 0 secrets**. Trivy inventoried two
targets. This is a bounded known-vulnerability scan, not proof of zero unknown
vulnerabilities; CI repeats it and fails on new CRITICAL/HIGH or secret findings.

## OCR decision for the MVP

The main runtime installs only locked non-OCR dependencies. It runs a pure
Python `pypdf` text-layer reader in the existing bounded, killable processing
child and then invokes the unchanged, private deterministic fiscal extractor.
It neither downloads models nor fabricates OCR content. PDFs without selectable
text fail explicitly; visual tables and scanned/image-only PDFs are **not** a
supported commercial input in this MVP. `fake` remains test/development-only.
Production refuses `fake`, `paddle` and `ppstructure`.

The optional, non-production `ocr` extra remains in the lockfile for future
research, but is not in the runtime image. A 2026-10-01 public dependency
audit of that extra (excluding the private extractor) returned 11 records in
4 packages / 8 distinct advisory IDs: `imgaug` PYSEC-2026-356
(CVE-2026-31235, CRITICAL), `paddlepaddle` PYSEC-2026-1754/1756,
`protobuf` PYSEC-2026-1805/1806, and `urllib3` CVE-2026-97687/97688/97689.
The `urllib3` 2.7.0 issues are fixed in 2.8.0; this does not resolve the
other OCR-stack issues. Do not install the extra into the production image or
activate native OCR until a separately isolated, patched design is reviewed.
The audited main runtime public dependency set had **0 known advisories**.
Public indexes do not audit the proprietary extractor source; its installed
commit still equals the lockfile's
`ddb485ff76627f2e995b11d2b4d11325fc5628c9`.

## Tenant ownership and migration

`PAPELA_TENANT_API_KEYS` is a JSON map of canonical tenant UUID to one unique
strong key. Production rejects the historical shared `PAPELA_API_KEYS` setting.
Authentication resolves the principal before the body is admitted; no raw key
is logged or stored in a job. Every uploaded job records `tenant_id UUID NOT
NULL`. Status, result and deletion queries include both job UUID and tenant
UUID. Cross-tenant and unknown jobs both return 404; an owner's active job
cannot be deleted (409). There is no public retry endpoint. The worker's
unscoped lookup is internal and follows its server-side queue claim.

Apply [`0002_tenant_isolation.sql`](../app/migrations/0002_tenant_isolation.sql)
**once as the schema owner before starting production API/worker**, using its
documented transactional `psql` command. It adds/backfills the owner column,
sets NOT NULL, adds a tenant/creation-time index, and installs an immutable
ownership trigger. Historical rows move to reserved quarantine tenant
`00000000-0000-0000-0000-000000000001`, which cannot be configured as an API
principal. The migration neither deletes jobs nor guesses a customer owner.
It is safe to rerun. Development/test startup applies it automatically;
production startup checks the column and trigger and retains its existing
least-privilege DB-role checks. Reassigning historical jobs requires a
separate reviewed operation; it is not part of this gate.

## Local evidence and release boundary

- `uv lock --check`: PASS, 93 locked packages.
- `ruff check .`: PASS; `mypy app`: PASS, 16 files.
- `pytest -ra`: **77 passed, 0 failed, 0 skipped**; two third-party
  deprecation warnings. The synthetic worker was stopped during the suite so
  it could not race tests that manually process queued jobs.
- `uv build`: sdist/wheel PASS; both Compose configurations: PASS.
- Fresh `docker build --no-cache` with BuildKit SSH forwarding: PASS; private
  extractor revision check inside runtime: installed=locked commit above.
- Disposable API/worker E2E: PASS for `/health`, upload, extraction, result,
  cross-tenant 404, owner deletion and PDF purge. Negative smoke: PASS for
  missing/invalid auth, unsafe IDs, host/header control and malformed/large PDFs.
- `docker history` sensitive marker scan: false. Runtime checks: no SSH home,
  Git, SSH, shell, mount, nsenter, infocmp, Perl, pip/setuptools/wheel, or
  importable Paddle/imgaug. `/data/pdfs` is UID 65532, mode 0700.
- `git diff --check`: PASS before commit. A staged diff/secret review and a
  successful GitHub Actions run for the exact commit are release requirements.

All smoke endpoints are allowlisted to the isolated synthetic Compose stack;
no production service or normal development data was touched. The scanner
consumed a local `docker save` archive, without a Docker socket mount or image
publication. Remove local archives after evidence collection; no runtime image
or proprietary source is uploaded as a CI artifact.

Remaining non-Gate-3 production prerequisites include provisioned DB role and
migration execution, deployment/TLS controls, backup/restore, operational
monitoring, measured extraction accuracy, and a documented retention policy
for extracted JSON. These belong to later gates. **Do not deploy yet.**

# Gate 3 — security hardening (2026-09-27)

Status: **BLOCKED**, not production-ready. This gate only changes the local API,
worker, tests and build. No production infrastructure, extractor source, SSH
deploy key or GitHub secret was changed. Do not deploy this as a commercial SaaS.

## Scope and trust boundary

This remains a **single shared-trust API**. Every configured API key can read
every job. There is no user, company or tenant ownership model, so providing
different keys to mutually untrusted customers would expose their documents.
Only operators in one trust domain may use it. Multi-tenancy requires an
explicit product design, resource ownership and isolation tests; this gate does
not pretend a UUID or an API key is tenant authorization. Cookie sessions do
not exist, so CSRF is not applicable. The service makes no request to a
client-supplied URL, so no SSRF path was found. SQL uses bound parameters and
there is no client-controlled shell execution or client-controlled JSONB field.

The production startup now requires strong API keys, non-example database
credentials and a non-privileged DB role that cannot own/alter the jobs table
or create schema objects. Schema bootstrap remains development/test-only;
fake OCR and disabled raw-PDF purge are rejected in production;
production provisioning/migration by a separate owner is a prerequisite.
The default Compose is a **loopback-only development sample**, not a deployment
recipe. It still contains conspicuous sample DB credentials and fake OCR.
Production needs separately provisioned secrets, schema, TLS reverse proxy,
backup/restore, monitoring and deployment controls in later gates.

## Risks discovered before implementation

The initial code inspection found CRITICAL **0**, HIGH **6**, MEDIUM **5**, LOW
**2**. High: unauthenticated multipart/body admission, no killable PDF/OCR
deadline, DB-supplied worker paths, terminal failure retaining PDFs, raw
exception logs, and all-interface dev Compose ports with example credentials.
Medium: non-constant-time keys; unchecked IDs/request IDs; absent headers/host
control; orphan PDF on failed INSERT; deletion audit stamped before deletion.
Low: public API docs and unconstrained configuration values.

Later dependency scans added **15 distinct HIGH advisory IDs** to triage:
10 in the original runtime image (8 Debian, 2 Python build tools) and 5 in
the optional OCR lock set. This makes 21 initial high-risk findings/advisories
in total, not 21 separate remotely exploitable API vulnerabilities. Scanner
severity is distinct from our contextual exploitability assessment.

## Changes and test mapping

| Files | Vulnerability and mitigation | Verification |
| --- | --- | --- |
| `app/http_security.py`, `app/security.py`, `app/main.py` | Authenticate, rate-limit and cap request bodies before multipart; constant-time key comparison and reject duplicate/malformed headers | credential, duplicate-header, pre-read, chunked/slow body and rate-limit tests |
| `app/storage.py`, `app/processing.py`, `app/worker.py` | Check PDF magic and parse in killable subprocess; cap bytes/pages/results/time, bind paths to server UUID, use exclusive owner-only files; bound OCR in credential-cleared child | invalid/empty/corrupt/oversize/mime/path/deadline/arbitrary-DB-path tests |
| `app/worker.py`, `app/repository.py`, `app/main.py` | Delete after terminal failure, stamp audit only after deletion, clean orphan on failed INSERT; retry deletion from sweep | purge, retention, failed-job and DB-failure tests |
| `app/logging_config.py`, `app/main.py`, `app/http_security.py` | Do not emit exception text/traceback or echo invalid inputs; keep UUID request IDs and safe event/error category | log and 500/422 response tests |
| `app/config.py`, `app/db.py`, `Dockerfile`, `docker-compose.yml` | Validate security limits, reject privileged production DB roles, bound DB statements, remove runtime packaging tools, loopback-bind dev ports and disable proxy/access-log trust | config, DB-role, Compose, image scan and smoke tests |
| `.gitignore`, `.dockerignore` | Exclude environment/key material and build caches | tracked-file review, Gitleaks, image scan |

Both Compose variants drop Linux capabilities and disable privilege gain for
API/worker. This reduces exposure to local mount/privilege-escalation CVEs;
it is **not** a blanket dismissal of the scanner's remaining Debian findings.

The API never uses client filenames as storage paths. Rejected/failed uploads
are removed. Internal files use mode 0600 and a private directory when newly
created; volumes must also be provisioned with private permissions. Retention
deletes raw PDFs, not extracted JSON; deletion of JSON/history and legal
retention policy remain separate LGPD work.

## Dependency and secret evidence

Tools used: Gitleaks 8.30.1; pip-audit 2.10.0; Trivy 0.74.0, from official
releases with SHA-256 checksums checked before execution. Pip-audit found no
known advisories in the installed development environment, but this does **not**
cover optional OCR or Debian packages. A second audit exported all lockfile
extras while excluding the private package (no source transmitted); it found
8 records / 5 unique advisory IDs in optional OCR packages:

| Package | Locked version | Advisory / CVE | Context |
| --- | --- | --- | --- |
| imgaug | 0.4.0 | PYSEC-2026-356 / CVE-2026-31235 | Insecure deserialization; no listed fix |
| paddlepaddle | 2.6.2 | PYSEC-2026-1754 / CVE-2024-0817; PYSEC-2026-1756 / CVE-2024-0815 | Command-injection helpers; no proven exposure via current app call path |
| protobuf | 3.20.2 | PYSEC-2026-1806 / CVE-2025-4565; PYSEC-2026-1805 / CVE-2026-0994 | Denial of service in parse/JSON paths; duplicated marker entries count once |

The default Docker image does **not** install the OCR extra. Do not enable real
PaddleOCR in production until compatible fixes or audited isolation are proven.
The private extractor is pinned at `ddb485ff76627f2e995b11d2b4d11325fc5628c9`
and is unavailable to public advisory indexes; no conclusion about its own
vulnerability status is implied.

The first Trivy image scan found 156 Debian advisories (44 HIGH) and 9 Python
advisories (2 HIGH) across 10 unique HIGH CVEs. The two Python HIGHs were
`jaraco.context` CVE-2026-23949 and `wheel` CVE-2026-24049, brought by global
packaging tools; they are removed from the runtime in the final Dockerfile.
The 8 Debian HIGH CVEs remain under triage: CVE-2025-69720,
CVE-2026-16742, CVE-2026-54369, CVE-2026-76642, CVE-2026-78408,
CVE-2026-78409, CVE-2026-78410 and CVE-2026-9538. The Debian tracker says
some require local privileged utilities or systemd-homed (not exercised by the
non-root app), but not all have been demonstrated unreachable in this image.
The Trixie base is pinned by digest; no package mass-upgrade or unreviewed
base substitution was made. Re-scan the final image and obtain fixed package
versions or concrete per-CVE mitigation before a production claim. The final
fresh image scan still found 156 Debian advisories, including 44 HIGH
occurrences across those same 8 IDs, but **zero Python advisories and zero
secret findings**. The runtime does not contain importable pip, setuptools or
wheel. Image digest:
`sha256:e97a5c9f0b0aa6be08beaf89468bd068106167aa67e5f37cc6852e4cf1b5923a`.

Gitleaks scanned 11 recent commits and the working directory. Its one finding
in each scan points to the same **documented example API key** in `README.md`
(current and historical lines). The value was verified to match a local
development placeholder; no potentially real credential was reproduced here.
Tracked files include `.env.example` only; `.env`, private keys, data and caches
are ignored. Image secret scan found zero findings. These are bounded pattern
scans, not proof that arbitrary unknown secret formats cannot exist.

## Residual risks and hard gates

- **HIGH / BLOCKED:** unresolved base-image OS advisories above. Public
  exposure must wait for fixed packages or a documented, tested mitigation.
- **HIGH for real OCR / BLOCKED:** optional OCR extra contains the five advisory
  IDs above and its native models were not exercised. The current fake-OCR
  image is only an integration baseline.
- **HIGH if multiple customers / BLOCKED:** shared API-key trust domain grants
  all jobs to all valid keys; do not issue keys to isolated tenants.
- **MEDIUM:** quota/rate counters are per API process; horizontal scaling needs
  cross-process admission control. Limit API to one process until implemented.
- **HIGH for real OCR / BLOCKED:** no isolated PDF/OCR container or native
  parser sandbox. Killable child and resource limits reduce CPU/memory abuse,
  but a native parser exploit can still reach the worker trust boundary.
- **MEDIUM:** malware scanning is not implemented; assess realistic threat
  model and scanning requirements before accepting third-party documents.
- **MEDIUM:** production role/migrations, at-rest encryption, DB backup and
  extracted-JSON retention still require later gates. HTTP HSTS belongs at
  a verified TLS terminator, not in this HTTP-only application.

## Reproduction

Run `uv lock --check`, `uv run --no-sync ruff check .`,
`uv run --no-sync mypy app`, `uv run --no-sync pytest -ra` against the isolated
`compose.gate1.yml` Postgres on port 55433; no skipped tests are acceptable.
Then run `uv build`, both Compose config checks, a fresh Docker build with
BuildKit SSH forwarding, `scripts/verify_extractor.py` inside the image, and
`scripts/smoke_ci.py` plus `scripts/smoke_security.py` against the isolated
API/worker. The scanner commands and raw JSON results are kept only under
ignored `data/` or a local temporary
directory; never upload image/runtime artifacts containing private IP.

Executed local checks: lock PASS (93 packages); Ruff PASS; mypy PASS (15
modules); pytest **67 passed, 0 failed, 0 skipped** (two pre-existing
deprecation warnings); `uv build` PASS; both Compose configs PASS; fresh
Docker build and private-commit checker PASS; smoke E2E and negative HTTP
smoke PASS. The Linux image is not published. GitHub Actions must be checked
again after this change is pushed; a successful CI job does not override the
residual HIGH findings above.

This report records a partial security improvement with unresolved findings;
it is not a go-live approval.

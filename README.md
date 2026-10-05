# PAPELA.AI

> Gate 3 security evidence is in
> [the security audit](docs/gate3-security.md). The default Compose is a
> loopback-only development sample, not a production deployment.

> The isolated single-VPS infrastructure candidate and its operational
> limits are documented in [Gate 4](docs/gate4-production.md). The
> [Gate 5 observability design](docs/gate5-observability.md) adds private
> metrics and privacy-minimized logs. The [Gate 6 backup/restore runbook](docs/gate6-backup-restore.md)
> covers logical database recovery. The [Gate 7 release/rollback runbook](docs/gate7-deploy-rollback.md)
> defines a localhost-only deploy drill. [Gate 8](docs/gate8-data-lifecycle.md)
> documents the synthetic erasure journal and retention controls. The
> [Gate 9 go-live decision](docs/gate9-go-live-readiness.md) is **NO-GO** for
> customer data; no VPS, offsite restore, registry digest or live alerting
> has been verified.

On-premise fiscal-document pipeline. The safe MVP path extracts embedded text
from digital PDFs; native PaddleOCR remains optional and is **not** permitted
in the production image while its dependency advisories remain unresolved.
Structured output lands in Postgres. No document is sent to a third-party API.

## Architecture

    client --POST /v1/upload--> API (authenticate tenant, validate, store, insert job=pending) --> 202 job_id
                                                        |
                                                   Postgres (jobs table = queue)
                                                        |
    worker  --claim (FOR UPDATE SKIP LOCKED)--> run OCR --> job=done (result JSONB) --> purge PDF
                                                        |                              / retry / failed
    client --GET /v1/jobs/{id}--> poll status/result

- Async by design: OCR is CPU-bound (seconds–minutes); never runs in the request path.
- Queue = Postgres `SELECT ... FOR UPDATE SKIP LOCKED`. No Redis/Celery. On-prem.
- Processing behind `OcrEngine`: `text` (digital PDFs, production MVP), `fake`
  (dev/CI only), or native PaddleOCR (disabled in production).
- API + worker are the SAME image; compose runs them as separate services on a
  shared `papela_pdfs` volume (API writes, worker reads + purges).

## Endpoints

- `GET  /health`             unauthenticated liveness probe
- `GET  /readiness`          Postgres + private storage readiness probe
- `POST /v1/upload`          multipart `file` (PDF); returns 202 + job_id
- `GET  /v1/jobs/{id}`       job status + result
- `GET  /v1/jobs/{id}/audit` LGPD audit: `file_exists` + `purged_at`
- `DELETE /v1/jobs/{id}`     delete an owned terminal job and its raw PDF

Auth: `X-API-Key` header. In production, `PAPELA_TENANT_API_KEYS` must be a
JSON object mapping stable tenant UUIDs to unique strong keys; the legacy
comma-separated `PAPELA_API_KEYS` is **development/test only**. Every job has an
immutable `tenant_id`, and cross-tenant reads/deletes return 404. Rate limits
are per tenant, in-process. See the migration procedure in
[the security audit](docs/gate3-security.md) before upgrading any existing DB.

## Data lifecycle and privacy boundaries

Both the raw PDF and the extracted result can contain personal/fiscal data;
the pipeline minimizes the raw file's lifetime on disk.

- Data minimization (`PAPELA_PURGE_AFTER_DONE`, fail-safe default `true`): the
  worker persists the extracted text and then deletes the raw PDF from disk, so a
  `done` job leaves no sensitive file behind. Set to `false` only in dev when you
  must inspect the stored file.
- Crash safety: the result is written (`mark_done`) BEFORE the PDF is deleted, so
  a crash can at worst orphan a PDF (reclaimed by the retention sweep) — never
  purge a file with no persisted result.
- Retention sweep: every `PAPELA_PURGE_INTERVAL_S` (default 1h) the worker deletes
  raw PDFs of `done` jobs older than `PAPELA_RETENTION_DAYS` (default 7).
- Crash recovery (reaper): jobs stuck in `processing` past `PAPELA_STALL_TIMEOUT_S`
  (default 300s) — a worker that died mid-job — are requeued for retry, or failed
  once `max_attempts` is reached. Runs in the same maintenance loop as the sweep.
- PII-safe errors: OCR/parse exceptions can embed document content, so the stored
  `error` column and logs contain only an allow-listed category + exception class
  name (see `app/sanitize.py`), never the raw exception message.
- Auditability: every deletion emits a structured log line (`pdf.deleted`, with
  job_id + reason) and stamps `jobs.purged_at`. `GET /v1/jobs/{id}/audit`
  cross-checks the DB stamp against the actual filesystem.
- Extracted JSON stays in Postgres until owner deletion, operator offboarding
  or the worker's configured terminal-job retention sweep (technical default
  30 days). Erasure markers and restore reconciliation prevent an older
  backup from silently reviving deleted records; production journal
  compaction remains disabled. Technical defaults are **not** an approved
  legal retention policy. See [Gate 8](docs/gate8-data-lifecycle.md).
- Processing, queue and storage are local. No third-party document API calls.

Relevant env vars (see `.env.example`):

    PAPELA_PURGE_AFTER_DONE   true|false   delete raw PDF right after OCR (default true)
    PAPELA_RETENTION_DAYS     int (7)      max age of raw PDFs on disk
    PAPELA_PURGE_INTERVAL_S   int (3600)   maintenance loop period (sweep + reaper)
    PAPELA_STALL_TIMEOUT_S    int (300)    requeue jobs stuck in 'processing' this long

## Run (Docker development sample, all 3 services)

    # Loopback API :8077, Postgres :5433; synthetic credentials and fake OCR.
    # Do not use this Compose as a production deployment.
    docker compose build --ssh default
    docker compose up -d

    curl -s -X POST http://localhost:8077/v1/upload \
      -H "X-API-Key: dev-key-change-me" -F "file=@nota.pdf;type=application/pdf"
    docker compose logs -f worker      # watch job.claimed / pdf.deleted / job.done

## Run (dev, without Docker)

    cp .env.example .env
    docker compose up -d postgres --wait   # just Postgres (host :5433)
    uv venv --python 3.11
    uv sync --locked --extra dev          # API/worker/tests (fake OCR)
    # The `ocr` extra is not approved for production; do not add it to the image.

    uv run uvicorn app.main:app --reload           # API
    uv run python -m app.worker                    # worker (separate process)

## Test

    docker compose up -d postgres --wait
    uv run pytest                          # fails if Postgres is unavailable

## Quality checks

    uv lock --check
    uv sync --locked --extra dev
    uv run --locked --extra dev ruff check .
    uv run --locked --extra dev mypy app
    uv run --locked --extra dev pytest
    uv build
    uv run --locked --extra dev python scripts/verify_extractor.py

The checks above are the local Gate 1 baseline. The Docker image requires SSH
forwarding only while resolving the private extractor during the build:

    docker compose build --ssh default

Docker installs with `uv sync --locked --no-dev --no-editable`: a missing or
inconsistent lockfile fails the build. SSH is forwarded only into the builder;
the runtime receives the installed environment, lockfile and provenance check,
without SSH tools or the Git checkout cache. Runtime Python uses `/app/.venv`.

For a fresh reproducibility check:

    docker build --no-cache --pull --ssh default -t papelaai-gate1 .
    docker run --rm --network none papelaai-gate1 python scripts/verify_extractor.py

`v0.1.0` is an annotated tag: `2746d39c395d2c09849f938a4cee0fc4d8208358`
is its tag-object SHA; its peeled commit is
`ddb485ff76627f2e995b11d2b4d11325fc5628c9`, the commit pinned in `uv.lock`.
The two hashes describe different Git object types, not different code revisions.
The remote tag and peeled commit matched these values during revalidation.
No tag or private extractor source was changed.

## Fiscal field extraction (proprietary, private dependency)

After OCR, the worker calls `papela_fiscal_extractor.extract_fields(...)` over
the OCR payload and attaches the result to `result["fields"]` (schema v3).
100% deterministic — no LLM, no external network calls (LGPD: runs entirely
on the already-local OCR result). It never raises: any internal failure
degrades to null/empty per field and is logged without document content.

**This extraction core is proprietary and lives outside this repository.**
The exact fields, rules and heuristics are part of PAPELA.AI's commercial
differentiator and are intentionally not documented here. The JSON shape
`result["fields"]` produces is exercised end-to-end in `tests/test_api.py`.

**Development note:** this repository does not build or run the worker
without `papela-fiscal-extractor` available as a local dependency (see
`pyproject.toml`'s direct Git dependency and `uv.lock`). There is no public fallback/stub
implementation by design.

## Scope / next steps

- Extraction is heuristic (label + table driven). Accuracy must be measured
  against a real anonymized DANFE/NFS-e corpus before any SLA claim; add a
  labeled fixture set + per-field accuracy report.
- Known limits: rate limiter is per-process (single node); move to
  Postgres/Redis to scale out. The explicit tenant migration must be applied
  by a schema owner before production startup. The worker's automatic
  terminal-job/result sweep is a technical default, not a legal policy.

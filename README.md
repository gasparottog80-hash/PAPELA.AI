# PAPELA.AI

> Security readiness: Gate 3 is **BLOCKED**; see
> [the security audit](docs/gate3-security.md). The current API has one shared
> trust domain: any valid key can access every job. Do not use it for isolated
> customers or expose the development Compose to the Internet.

On-premise OCR pipeline for fiscal documents (notas, contratos). Upload a PDF,
a local worker runs OCR (PaddleOCR, Apache-2.0), and structured output lands in
Postgres. No data leaves the host — LGPD-friendly.

## Architecture

    client --POST /v1/upload--> API (validate + store to disk, insert job=pending) --> 202 job_id
                                                        |
                                                   Postgres (jobs table = queue)
                                                        |
    worker  --claim (FOR UPDATE SKIP LOCKED)--> run OCR --> job=done (result JSONB) --> purge PDF
                                                        |                              / retry / failed
    client --GET /v1/jobs/{id}--> poll status/result

- Async by design: OCR is CPU-bound (seconds–minutes); never runs in the request path.
- Queue = Postgres `SELECT ... FOR UPDATE SKIP LOCKED`. No Redis/Celery. On-prem.
- OCR behind `OcrEngine`: `fake` (dev/CI, no model download) or `paddle` (real).
- API + worker are the SAME image; compose runs them as separate services on a
  shared `papela_pdfs` volume (API writes, worker reads + purges).

## Endpoints

- `GET  /health`             unauthenticated liveness probe
- `POST /v1/upload`          multipart `file` (PDF); returns 202 + job_id
- `GET  /v1/jobs/{id}`       job status + result
- `GET  /v1/jobs/{id}/audit` LGPD audit: `file_exists` + `purged_at`

Auth: `X-API-Key` header. Fail-closed — in `production` the app refuses to start
without `PAPELA_API_KEYS` set. Rate limit: per-key fixed window (in-process).

## LGPD compliance

The raw PDF is the sensitive artifact; the pipeline minimizes its lifetime on disk.

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
- What is NOT deleted here: the extracted text/JSON in Postgres. Purging that is a
  separate retention decision (add a DELETE-by-age sweep on the `jobs` table when
  the legal retention period for the extracted data is defined).
- Data never leaves the host: OCR is local (PaddleOCR), queue is Postgres, storage
  is a local volume. No third-party API calls.

Relevant env vars (see `.env.example`):

    PAPELA_PURGE_AFTER_DONE   true|false   delete raw PDF right after OCR (default true)
    PAPELA_RETENTION_DAYS     int (7)      max age of raw PDFs on disk
    PAPELA_PURGE_INTERVAL_S   int (3600)   maintenance loop period (sweep + reaper)
    PAPELA_STALL_TIMEOUT_S    int (300)    requeue jobs stuck in 'processing' this long

## Run (Docker, all 3 services)

    # api on host :8077, postgres on host :5433, worker in background
    docker compose build --ssh default
    PAPELA_ENV=production PAPELA_API_KEYS=change-me PAPELA_PURGE_AFTER_DONE=true \
      docker compose up -d

    curl -s -X POST http://localhost:8077/v1/upload \
      -H "X-API-Key: change-me" -F "file=@nota.pdf;type=application/pdf"
    docker compose logs -f worker      # watch job.claimed / pdf.deleted / job.done

## Run (dev, without Docker)

    cp .env.example .env
    docker compose up -d postgres --wait   # just Postgres (host :5433)
    uv venv --python 3.11
    uv sync --locked --extra dev          # API/worker/tests (fake OCR)
    # uv sync --locked --extra dev --extra ocr  # + real PaddleOCR runtime

    uv run uvicorn app.main:app --reload           # API
    uv run python -m app.worker                    # worker (separate process)

## Test

    docker compose up -d postgres --wait
    uv run pytest                          # skips gracefully if Postgres unreachable

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
  Postgres/Redis to scale out. No schema migrations tool yet (idempotent DDL
  on startup). No retention sweep on the extracted JSON yet (only on raw PDFs).

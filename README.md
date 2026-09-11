# PAPELA.AI

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

- Data minimization (`PAPELA_PURGE_AFTER_DONE=true`, default in production): the
  worker deletes the raw PDF from disk in the SAME step it persists the extracted
  text, so a `done` job never leaves a sensitive file sitting on disk.
- Retention sweep: every `PAPELA_PURGE_INTERVAL_S` (default 1h) the worker deletes
  raw PDFs of `done` jobs older than `PAPELA_RETENTION_DAYS` (default 7). This is
  the belt-and-suspenders path even when purge-after-done is off.
- Auditability: every deletion emits a structured log line (`pdf.deleted`, with
  job_id + reason) and stamps `jobs.purged_at`. `GET /v1/jobs/{id}/audit`
  cross-checks the DB stamp against the actual filesystem.
- What is NOT deleted here: the extracted text/JSON in Postgres. Purging that is a
  separate retention decision (add a DELETE-by-age sweep on the `jobs` table when
  the legal retention period for the extracted data is defined).
- Data never leaves the host: OCR is local (PaddleOCR), queue is Postgres, storage
  is a local volume. No third-party API calls.

Relevant env vars (see `.env.example`):

    PAPELA_PURGE_AFTER_DONE   true|false   delete raw PDF right after OCR
    PAPELA_RETENTION_DAYS     int (7)      max age of raw PDFs on disk
    PAPELA_PURGE_INTERVAL_S   int (3600)   retention sweep period

## Run (Docker, all 3 services)

    # api on host :8077, postgres on host :5433, worker in background
    PAPELA_ENV=production PAPELA_API_KEYS=change-me PAPELA_PURGE_AFTER_DONE=true \
      docker compose up -d --build

    curl -s -X POST http://localhost:8077/v1/upload \
      -H "X-API-Key: change-me" -F "file=@nota.pdf;type=application/pdf"
    docker compose logs -f worker      # watch job.claimed / pdf.deleted / job.done

## Run (dev, without Docker)

    cp .env.example .env
    docker compose up -d postgres --wait   # just Postgres (host :5433)
    uv venv --python 3.11
    uv pip install -e ".[dev]"             # API/worker/tests (fake OCR)
    # uv pip install -e ".[dev,ocr]"       # + real PaddleOCR runtime

    uv run uvicorn app.main:app --reload           # API
    uv run python -m app.worker                    # worker (separate process)

## Test

    docker compose up -d postgres --wait
    uv run pytest                          # skips gracefully if Postgres unreachable

## Scope / next steps

- OCR result today = per-page text + confidence in JSONB. Fiscal field extraction
  (CNPJ, valor, itens) is the deferred local-LLM classification step.
- Known limits: rate limiter is per-process (single node); move to Postgres/Redis
  to scale out. No schema migrations tool yet (idempotent DDL on startup). No
  retention sweep on the extracted JSON yet (only on raw PDFs).

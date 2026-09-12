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

## Fiscal field extraction (deterministic)

After OCR, the worker runs `app/extract.py` over the OCR payload (text +
structured tables) and attaches `result["fields"]`. 100% deterministic —
regex + heuristics + check-digit validation, no LLM, no external calls (LGPD:
runs on the already-local result). It never raises: any sub-extractor failure
degrades to null/empty and is logged without document content.

Fields (schema v3, `result.fields`):

- `emitente_cnpj` / `destinatario_cnpj` — `{value, digits, valid, role,
  role_source}`. `valid` is the mod-11 check-digit result (kills most regex
  false positives). `role_source` is `label` (from an explicit
  Emitente/Destinatário keyword) or `position` (two-unlabeled-CNPJ fallback).
- `cnpjs` / `cpfs` — every distinct document found, each check-digit validated.
- `numero_nf` — NF number; boundary-guarded so it never grabs a slice of the
  44-digit NFe access key.
- `data_emissao` — ISO date, label-anchored with a first-date fallback.
- `valor_total` — pt-BR money parsed to a `"1590.00"` string (exact, no float).
- `itens` — line items from structured tables: `{descricao, quantidade,
  valor_unitario, valor_total, codigo, raw}` (columns mapped from the header;
  money as strings). Unmappable tables are skipped, not turned into garbage.

## Scope / next steps

- Extraction is heuristic (label + table driven). Accuracy must be measured
  against a real anonymized DANFE/NFS-e corpus before any SLA claim; add a
  labeled fixture set + per-field accuracy report.
- Known limits: rate limiter is per-process (single node); move to
  Postgres/Redis to scale out. No schema migrations tool yet (idempotent DDL
  on startup). No retention sweep on the extracted JSON yet (only on raw PDFs).

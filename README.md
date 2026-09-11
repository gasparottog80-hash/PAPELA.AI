# PAPELA.AI

On-premise OCR pipeline for fiscal documents (notas, contratos). Upload a PDF,
a local worker runs OCR (PaddleOCR, Apache-2.0), and structured output lands in
Postgres. No data leaves the host — LGPD-friendly.

## Architecture

    client --POST /v1/upload--> API (validate + store to disk, insert job=pending) --> 202 job_id
                                                        |
                                                   Postgres (jobs table = queue)
                                                        |
    worker  --claim (FOR UPDATE SKIP LOCKED)--> run OCR --> job=done (result JSONB) / retry / failed
                                                        |
    client --GET /v1/jobs/{id}--> poll status/result

- Async by design: OCR is CPU-bound (seconds–minutes); never runs in the request path.
- Queue = Postgres `SELECT ... FOR UPDATE SKIP LOCKED`. No Redis/Celery. On-prem.
- OCR behind `OcrEngine`: `fake` (dev/CI, no model download) or `paddle` (real).

## Endpoints

- `GET  /health`            unauthenticated liveness probe
- `POST /v1/upload`         multipart `file` (PDF); returns 202 + job_id
- `GET  /v1/jobs/{id}`      job status + result

Auth: `X-API-Key` header. Fail-closed — in `production` the app refuses to start
without `PAPELA_API_KEYS` set. Rate limit: per-key fixed window (in-process).

## Run (dev)

    cp .env.example .env
    docker compose up -d --wait          # Postgres on host port 5433
    uv venv --python 3.11
    uv pip install -e ".[dev]"           # API/worker/tests (fake OCR)
    # uv pip install -e ".[dev,ocr]"     # + real PaddleOCR runtime

    uv run uvicorn app.main:app --reload           # API
    uv run python -m app.worker                    # worker (separate process)

## Test

    docker compose up -d --wait
    uv run pytest                        # skips gracefully if Postgres unreachable

## Scope / next steps

- OCR result today = per-page text + confidence in JSONB. Fiscal field extraction
  (CNPJ, valor, itens) is the deferred local-LLM classification step.
- Known limits: rate limiter is per-process (single node); move to Postgres/Redis
  to scale out. No schema migrations tool yet (idempotent DDL on startup).

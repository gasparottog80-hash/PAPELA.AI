from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request, Response, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import get_settings
from .db import Database
from .error_codes import (
    HTTP_ERROR_CODES,
    INVALID_PDF,
    INVALID_REQUEST,
    JOB_NOT_FOUND,
    STORAGE_UNAVAILABLE,
    UPLOAD_TOO_LARGE,
)
from .http_security import SecurityBoundary
from .metrics import METRICS, start_metrics_server
from .repository import JobRepository
from .schemas import ErrorResponse, JobStatus, UploadAccepted
from .security import RateLimiter, require_api_key
from .storage import (
    InvalidPdfError,
    delete_pdf,
    pdf_exists,
    safe_name,
    save_pdf_chunked,
)

logger = logging.getLogger("papela.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .logging_config import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level, "api")

    # A legacy shared-key list has no per-customer owner. Production only
    # accepts an explicit, unique tenant-to-key mapping.
    if settings.env == "production" and (
        settings.api_key_set or not settings.tenant_api_keys
    ):
        raise RuntimeError(
            "Production requires PAPELA_TENANT_API_KEYS without legacy keys"
        )
    if settings.env == "production":
        if any(
            len(key) < 32 or not key.isascii()
            for _, key in settings.tenant_key_pairs
        ):
            raise RuntimeError("Production requires strong ASCII API keys")
        if urlsplit(settings.database_url).password in {
            None,
            "papela",
            "gate1-test-only",
        }:
            raise RuntimeError("Production requires explicit database credentials")
        if settings.ocr_engine != "text":
            raise RuntimeError(
                "Production MVP requires text-layer extraction; native OCR is disabled"
            )
        if not settings.purge_after_done:
            raise RuntimeError("Production requires raw-PDF purge after completion")

    db = Database(settings)
    try:
        db.open()
    except Exception:
        METRICS.inc("db_errors_total", "startup")
        raise
    app.state.db = db
    app.state.repo = JobRepository(db.pool)
    app.state.rate_limiter = RateLimiter(settings.rate_limit_per_min)
    try:
        metrics_server = start_metrics_server(METRICS)
    except Exception:
        db.close()
        raise
    logger.info("startup", extra={"request_id": None})
    try:
        yield
    finally:
        metrics_server.shutdown()
        metrics_server.server_close()
        db.close()
        logger.info("shutdown", extra={"request_id": None})


app = FastAPI(
    title="PAPELA.AI",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=get_settings().allowed_hosts.split(","),
)
app.add_middleware(SecurityBoundary)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    messages = {
        400: "invalid request",
        401: "invalid api key",
        404: "not found",
        405: "method not allowed",
        409: "job is not terminal",
        413: "request too large",
    }
    code = HTTP_ERROR_CODES.get(exc.status_code, INVALID_REQUEST)
    if exc.status_code == 404 and not request.url.path.startswith("/v1/jobs/"):
        code = INVALID_REQUEST
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "request_id": request.state.request_id,
            "code": code,
            "error": messages.get(exc.status_code, "request rejected"),
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # FastAPI's default response echoes invalid inputs; do not echo credentials/PII.
    return JSONResponse(
        status_code=422,
        content={
            "request_id": request.state.request_id,
            "code": INVALID_REQUEST,
            "error": "invalid request",
        },
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readiness")
async def readiness(request: Request) -> Response:
    """Only route traffic when Postgres and the private PDF volume are usable."""
    settings = get_settings()
    storage = Path(settings.storage_dir)
    try:
        storage_ready = (
            storage.is_dir()
            and os.access(storage, os.W_OK | os.X_OK)
            and shutil.disk_usage(storage).free
            >= settings.max_upload_bytes + 1024 * 1024
        )
    except OSError:
        storage_ready = False
    db_ready = await run_in_threadpool(request.app.state.db.is_ready)
    if not storage_ready or not db_ready:
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return JSONResponse(content={"status": "ready"})


@app.post(
    "/v1/upload",
    response_model=UploadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={400: {"model": ErrorResponse}, 401: {}, 429: {}},
)
async def upload(
    request: Request,
    file: UploadFile,
    tenant_id: str = Depends(require_api_key),
):
    settings = get_settings()
    request_id: str = request.state.request_id
    started_at = time.monotonic()

    if file.content_type not in ("application/pdf", "application/octet-stream"):
        METRICS.inc("uploads_rejected_total", "invalid_pdf")
        logger.warning(
            "upload.rejected",
            extra={"request_id": request_id, "error_code": INVALID_PDF},
        )
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                request_id=request_id,
                code=INVALID_PDF,
                error="content-type must be application/pdf",
            ).model_dump(),
        )

    repo: JobRepository = request.app.state.repo
    job_id = str(uuid.uuid4())
    try:
        storage_path, size, pages = save_pdf_chunked(
            stream=file.file,
            job_id=job_id,
            storage_dir=settings.storage_dir,
            max_bytes=settings.max_upload_bytes,
            max_pages=settings.max_pdf_pages,
        )
    except InvalidPdfError as exc:
        error_code = (
            UPLOAD_TOO_LARGE if exc.status_code == 413 else
            STORAGE_UNAVAILABLE if exc.status_code == 507 else INVALID_PDF
        )
        METRICS.inc(
            "uploads_rejected_total",
            "too_large" if exc.status_code == 413 else
            "storage" if exc.status_code == 507 else "invalid_pdf",
        )
        logger.warning(
            "upload.rejected",
            extra={"request_id": request_id, "error_code": error_code},
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                request_id=request_id, code=error_code, error=str(exc)
            ).model_dump(),
        )

    try:
        repo.create(
            job_id=job_id,
            tenant_id=tenant_id,
            filename=safe_name(file.filename or "upload.pdf"),
            storage_path=storage_path,
            size_bytes=size,
            pages=pages,
        )
    except Exception:
        delete_pdf(job_id, settings.storage_dir, reason="create-failed")
        raise
    METRICS.inc("jobs_created_total")
    logger.info(
        "upload.accepted",
        extra={
            "request_id": request_id,
            "job_id": job_id,
            "duration_ms": (time.monotonic() - started_at) * 1000,
        },
    )
    return UploadAccepted(job_id=job_id, status="pending", pages=pages)


@app.get(
    "/v1/jobs/{job_id}",
    response_model=JobStatus,
    responses={401: {}, 404: {"model": ErrorResponse}},
)
async def get_job(
    job_id: uuid.UUID,
    request: Request,
    tenant_id: str = Depends(require_api_key),
):
    repo: JobRepository = request.app.state.repo
    row = repo.get_for_tenant(str(job_id), tenant_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                request_id=request.state.request_id,
                code=JOB_NOT_FOUND,
                error="job not found",
            ).model_dump(),
        )
    return JobStatus(**row)


@app.get(
    "/v1/jobs/{job_id}/audit",
    responses={401: {}, 404: {"model": ErrorResponse}},
)
async def get_job_audit(
    job_id: uuid.UUID,
    request: Request,
    tenant_id: str = Depends(require_api_key),
):
    """LGPD audit view: whether the raw PDF still exists on disk vs. purged.
    Cross-checks the DB purged_at stamp against the actual filesystem so
    drift (file gone but not stamped, or vice-versa) is visible."""
    repo: JobRepository = request.app.state.repo
    row = repo.get_for_tenant(str(job_id), tenant_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                request_id=request.state.request_id,
                code=JOB_NOT_FOUND,
                error="job not found",
            ).model_dump(),
        )
    settings = get_settings()
    return {
        "job_id": row["id"],
        "status": row["status"],
        "purged_at": row["purged_at"].isoformat() if row["purged_at"] else None,
        "file_exists": pdf_exists(str(job_id), settings.storage_dir),
    }


@app.delete("/v1/jobs/{job_id}", status_code=204)
async def delete_job(
    job_id: uuid.UUID,
    request: Request,
    tenant_id: str = Depends(require_api_key),
):
    """Delete a tenant's terminal result and raw PDF; active jobs cannot race it."""
    repo: JobRepository = request.app.state.repo
    row = repo.get_for_tenant(str(job_id), tenant_id)
    if row is None:
        raise HTTPException(status_code=404)
    if row["status"] not in ("done", "failed"):
        raise HTTPException(status_code=409)
    delete_pdf(str(job_id), get_settings().storage_dir, reason="tenant-delete")
    if not repo.delete_terminal_for_tenant(str(job_id), tenant_id):
        raise HTTPException(status_code=409)
    return Response(status_code=204)

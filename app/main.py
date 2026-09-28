from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import get_settings
from .db import Database
from .http_security import SecurityBoundary
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
    configure_logging(settings.log_level)

    # Fail-closed: refuse to serve in prod without API keys configured.
    if settings.env == "production" and not settings.api_key_set:
        raise RuntimeError(
            "PAPELA_API_KEYS is empty in production; refusing to start (fail-closed)"
        )
    if settings.env == "production":
        if any(len(key) < 32 or not key.isascii() for key in settings.api_key_set):
            raise RuntimeError("Production requires strong ASCII API keys")
        if urlsplit(settings.database_url).password in {
            None,
            "papela",
            "gate1-test-only",
        }:
            raise RuntimeError("Production requires explicit database credentials")
        if settings.ocr_engine == "fake":
            raise RuntimeError("Fake OCR is forbidden in production")
        if not settings.purge_after_done:
            raise RuntimeError("Production requires raw-PDF purge after completion")

    db = Database(settings)
    db.open()
    app.state.db = db
    app.state.repo = JobRepository(db.pool)
    app.state.rate_limiter = RateLimiter(settings.rate_limit_per_min)
    logger.info("startup", extra={"request_id": None})
    try:
        yield
    finally:
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
        413: "request too large",
    }
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "request_id": request.state.request_id,
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
            "error": "invalid request",
        },
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/v1/upload",
    response_model=UploadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={400: {"model": ErrorResponse}, 401: {}, 429: {}},
)
async def upload(
    request: Request,
    file: UploadFile,
    api_key: str = Depends(require_api_key),
):
    settings = get_settings()
    request_id: str = request.state.request_id

    if file.content_type not in ("application/pdf", "application/octet-stream"):
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                request_id=request_id, error="content-type must be application/pdf"
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
        logger.warning("upload.rejected", extra={"request_id": request_id})
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(request_id=request_id, error=str(exc)).model_dump(),
        )

    try:
        repo.create(
            job_id=job_id,
            filename=safe_name(file.filename or "upload.pdf"),
            storage_path=storage_path,
            size_bytes=size,
            pages=pages,
        )
    except Exception:
        delete_pdf(job_id, settings.storage_dir, reason="create-failed")
        raise
    logger.info("upload.accepted", extra={"request_id": job_id})
    return UploadAccepted(job_id=job_id, status="pending", pages=pages)


@app.get(
    "/v1/jobs/{job_id}",
    response_model=JobStatus,
    responses={401: {}, 404: {"model": ErrorResponse}},
)
async def get_job(
    job_id: uuid.UUID,
    request: Request,
    api_key: str = Depends(require_api_key),
):
    repo: JobRepository = request.app.state.repo
    row = repo.get(str(job_id))
    if row is None:
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                request_id=request.state.request_id, error="job not found"
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
    api_key: str = Depends(require_api_key),
):
    """LGPD audit view: whether the raw PDF still exists on disk vs. purged.
    Cross-checks the DB purged_at stamp against the actual filesystem so
    drift (file gone but not stamped, or vice-versa) is visible."""
    repo: JobRepository = request.app.state.repo
    row = repo.get(str(job_id))
    if row is None:
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                request_id=request.state.request_id, error="job not found"
            ).model_dump(),
        )
    settings = get_settings()
    return {
        "job_id": row["id"],
        "status": row["status"],
        "purged_at": row["purged_at"].isoformat() if row["purged_at"] else None,
        "file_exists": pdf_exists(str(job_id), settings.storage_dir),
    }

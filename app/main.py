from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import Database
from .repository import JobRepository
from .schemas import ErrorResponse, JobStatus, UploadAccepted
from .security import RateLimiter, require_api_key
from .storage import InvalidPdfError, _safe_name, save_and_validate

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


app = FastAPI(title="PAPELA.AI", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    try:
        response = await call_next(request)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - last-resort structured 500
        logger.exception("unhandled", extra={"request_id": request_id})
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(request_id=request_id, error="internal error").model_dump(),
        )
    response.headers["x-request-id"] = request_id
    return response


def _enforce_rate_limit(request: Request, api_key: str) -> None:
    if not request.app.state.rate_limiter.allow(api_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded"
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
    _enforce_rate_limit(request, api_key)
    settings = get_settings()
    request_id: str = request.state.request_id

    if file.content_type not in ("application/pdf", "application/octet-stream"):
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                request_id=request_id, error="content-type must be application/pdf"
            ).model_dump(),
        )

    try:
        storage_path, size, pages = save_and_validate(
            stream=file.file,
            filename=file.filename or "upload.pdf",
            storage_dir=settings.storage_dir,
            max_bytes=settings.max_upload_bytes,
            max_pages=settings.max_pdf_pages,
        )
    except InvalidPdfError as exc:
        logger.warning("upload.rejected", extra={"request_id": request_id})
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(request_id=request_id, error=str(exc)).model_dump(),
        )

    repo: JobRepository = request.app.state.repo
    job_id = repo.create(
        filename=_safe_name(file.filename or "upload.pdf"),
        storage_path=storage_path,
        size_bytes=size,
        pages=pages,
    )
    logger.info("upload.accepted", extra={"request_id": job_id})
    return UploadAccepted(job_id=job_id, status="pending", pages=pages)


@app.get(
    "/v1/jobs/{job_id}",
    response_model=JobStatus,
    responses={401: {}, 404: {"model": ErrorResponse}},
)
async def get_job(
    job_id: str,
    request: Request,
    api_key: str = Depends(require_api_key),
):
    _enforce_rate_limit(request, api_key)
    repo: JobRepository = request.app.state.repo
    row = repo.get(job_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                request_id=request.state.request_id, error="job not found"
            ).model_dump(),
        )
    return JobStatus(**row)

"""Stable, safe error codes shared by HTTP responses and internal logs."""

from __future__ import annotations

from psycopg import Error as PostgresError
from psycopg_pool import PoolTimeout

from .sanitize import sanitize_error

AUTH_INVALID = "AUTH_INVALID"
UPLOAD_TOO_LARGE = "UPLOAD_TOO_LARGE"
INVALID_PDF = "INVALID_PDF"
INVALID_REQUEST = "INVALID_REQUEST"
RATE_LIMITED = "RATE_LIMITED"
DB_UNAVAILABLE = "DB_UNAVAILABLE"
PROCESSING_TIMEOUT = "PROCESSING_TIMEOUT"
PROCESSING_FAILED = "PROCESSING_FAILED"
JOB_NOT_FOUND = "JOB_NOT_FOUND"
JOB_NOT_TERMINAL = "JOB_NOT_TERMINAL"
STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
INTERNAL_ERROR = "INTERNAL_ERROR"

SAFE_ERROR_CODES = frozenset(
    {
        AUTH_INVALID,
        UPLOAD_TOO_LARGE,
        INVALID_PDF,
        INVALID_REQUEST,
        RATE_LIMITED,
        DB_UNAVAILABLE,
        PROCESSING_TIMEOUT,
        PROCESSING_FAILED,
        JOB_NOT_FOUND,
        JOB_NOT_TERMINAL,
        STORAGE_UNAVAILABLE,
        INTERNAL_ERROR,
    }
)

HTTP_ERROR_CODES = {
    400: INVALID_REQUEST,
    401: AUTH_INVALID,
    404: JOB_NOT_FOUND,
    405: INVALID_REQUEST,
    408: PROCESSING_TIMEOUT,
    409: JOB_NOT_TERMINAL,
    413: UPLOAD_TOO_LARGE,
    422: INVALID_REQUEST,
    429: RATE_LIMITED,
    500: INTERNAL_ERROR,
    503: DB_UNAVAILABLE,
    507: STORAGE_UNAVAILABLE,
}


def code_for_exception(exc: BaseException) -> str:
    """Classify without exposing exception text or document contents."""
    if isinstance(exc, (PostgresError, PoolTimeout)):
        return DB_UNAVAILABLE
    if isinstance(exc, TimeoutError):
        return PROCESSING_TIMEOUT
    if isinstance(exc, OSError):
        return STORAGE_UNAVAILABLE
    category = sanitize_error(exc).split(":", 1)[0]
    return {
        "ocr_timeout": PROCESSING_TIMEOUT,
        "dependency_unavailable": DB_UNAVAILABLE,
        "pdf_corrupt": INVALID_PDF,
        "pdf_encrypted": INVALID_PDF,
        "pdf_unreadable": INVALID_PDF,
        "resource_exhausted": PROCESSING_FAILED,
    }.get(category, PROCESSING_FAILED)

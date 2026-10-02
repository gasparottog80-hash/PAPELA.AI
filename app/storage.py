from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from .config import get_settings
from .error_codes import STORAGE_UNAVAILABLE
from .processing import bounded_process

logger = logging.getLogger("papela.storage")

PDF_MAGIC = b"%PDF-"


class InvalidPdfError(ValueError):
    """Raised when an upload is not a valid, in-limits PDF."""

    status_code = 400


class PdfTooLargeError(InvalidPdfError):
    status_code = 413


class StorageCapacityError(InvalidPdfError):
    status_code = 507


def get_pdf_path(job_id: str, storage_dir: str) -> str:
    """Deterministic on-disk path for a job's raw PDF.

    Derived only from the (server-generated) job_id, never the client
    filename, so there is no path-traversal surface.
    """
    name = str(UUID(job_id))
    root = Path(storage_dir).resolve()
    path = root / f"{name}.pdf"
    if path.is_symlink():
        raise InvalidPdfError("unsafe storage entry")
    return str(path)


def safe_name(filename: str) -> str:
    """Sanitized display-only filename (never used to build a path)."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(c for c in name if c.isprintable())[:255] or "upload.pdf"


def save_pdf_chunked(
    *,
    stream: BinaryIO,
    job_id: str,
    storage_dir: str,
    max_bytes: int,
    max_pages: int,
) -> tuple[str, int, int]:
    """Stream an upload to `{storage_dir}/{job_id}.pdf` with a hard byte cap,
    validate it is a real in-limits PDF, and return (path, size, pages).

    Fail-closed: the partial file is deleted on any rejection so a bad upload
    never leaves an orphan (nor a sensitive doc) on disk.
    """
    os.makedirs(storage_dir, mode=0o700, exist_ok=True)
    path = get_pdf_path(job_id, storage_dir)

    settings = get_settings()
    used = sum(p.stat().st_size for p in Path(storage_dir).glob("*.pdf"))
    if used + max_bytes > settings.max_storage_bytes:
        raise StorageCapacityError("storage capacity exceeded")
    if shutil.disk_usage(storage_dir).free < max_bytes + 1024 * 1024:
        raise StorageCapacityError("storage capacity exceeded")
    size = 0
    first = True
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                if first:
                    if not chunk.startswith(PDF_MAGIC):
                        raise InvalidPdfError("not a PDF (bad magic bytes)")
                    first = False
                size += len(chunk)
                if size > max_bytes:
                    raise PdfTooLargeError(f"file exceeds {max_bytes} bytes")
                out.write(chunk)

        if size == 0:
            raise InvalidPdfError("empty file")

        try:
            pages = int(bounded_process("validate", path, settings))
        except (ValueError, TimeoutError) as exc:
            raise InvalidPdfError("corrupt, encrypted or over-budget PDF") from exc

        if pages == 0:
            raise InvalidPdfError("PDF has no pages")
        if pages > max_pages:
            raise InvalidPdfError(f"PDF exceeds {max_pages} pages")

        return path, size, pages
    except Exception:
        delete_pdf(job_id, storage_dir, reason="rejected-upload")
        raise


def delete_pdf(job_id: str, storage_dir: str, *, reason: str) -> bool:
    """Delete a job's raw PDF from disk. Idempotent. Emits an auditable log
    line (job_id + reason) whether or not the file was present.

    Returns True if a file was actually removed.
    """
    path = get_pdf_path(job_id, storage_dir)
    try:
        os.remove(path)
        removed = True
    except FileNotFoundError:
        removed = False
    except OSError:
        logger.exception(
            "pdf.delete_failed",
            extra={
                "job_id": job_id,
                "cleanup_reason": reason,
                "error_code": STORAGE_UNAVAILABLE,
            },
        )
        raise
    logger.info(
        "pdf.deleted" if removed else "pdf.delete_noop",
        extra={"job_id": job_id, "cleanup_reason": reason},
    )
    return removed


def pdf_exists(job_id: str, storage_dir: str) -> bool:
    return os.path.exists(get_pdf_path(job_id, storage_dir))


def purge_expired_jobs(repo, storage_dir: str, *, retention_days: int) -> int:
    """Retention sweep: for every done job past retention, delete its raw PDF
    from disk and stamp purged_at. Idempotent; the extracted JSON stays in
    Postgres. Returns how many jobs were purged.

    `repo` is a JobRepository (untyped here to avoid a circular import).
    """
    job_ids = repo.expired_done_jobs(retention_days=retention_days)
    purged = 0
    for job_id in job_ids:
        delete_pdf(job_id, storage_dir, reason="retention-expired")
        repo.mark_purged(job_id)
        purged += 1
    if purged:
        logger.info("retention.sweep", extra={"request_id": None})
    return purged

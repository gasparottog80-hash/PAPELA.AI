from __future__ import annotations

import logging
import os
from typing import BinaryIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

logger = logging.getLogger("papela.storage")

PDF_MAGIC = b"%PDF-"


class InvalidPdfError(ValueError):
    """Raised when an upload is not a valid, in-limits PDF."""


def get_pdf_path(job_id: str, storage_dir: str) -> str:
    """Deterministic on-disk path for a job's raw PDF.

    Derived only from the (server-generated) job_id, never the client
    filename, so there is no path-traversal surface.
    """
    return os.path.join(storage_dir, f"{job_id}.pdf")


def safe_name(filename: str) -> str:
    """Sanitized display-only filename (never used to build a path)."""
    return os.path.basename(filename)[:255] or "upload.pdf"


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
    os.makedirs(storage_dir, exist_ok=True)
    path = get_pdf_path(job_id, storage_dir)

    size = 0
    first = True
    try:
        with open(path, "wb") as out:
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
                    raise InvalidPdfError(f"file exceeds {max_bytes} bytes")
                out.write(chunk)

        if size == 0:
            raise InvalidPdfError("empty file")

        try:
            pages = len(PdfReader(path).pages)
        except PdfReadError as exc:
            raise InvalidPdfError(f"corrupt PDF: {exc}") from exc

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
            extra={"request_id": job_id},
        )
        raise
    logger.info(
        "pdf.deleted" if removed else "pdf.delete_noop",
        extra={"request_id": job_id},
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

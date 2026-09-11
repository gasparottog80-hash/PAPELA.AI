from __future__ import annotations

import logging
import os
import uuid
from typing import BinaryIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

logger = logging.getLogger("papela.storage")

PDF_MAGIC = b"%PDF-"


class InvalidPdfError(ValueError):
    """Raised when an upload is not a valid, in-limits PDF."""


def _safe_name(filename: str) -> str:
    # Never trust client filename for the path; keep only for display.
    return os.path.basename(filename)[:255] or "upload.pdf"


def save_and_validate(
    *,
    stream: BinaryIO,
    filename: str,
    storage_dir: str,
    max_bytes: int,
    max_pages: int,
) -> tuple[str, int, int]:
    """Stream upload to disk with a hard byte cap, validate it is a PDF, and
    return (storage_path, size_bytes, pages). Deletes the file on any failure.

    Reads in chunks so a malicious huge upload can't blow up memory.
    """
    os.makedirs(storage_dir, exist_ok=True)
    storage_path = os.path.join(storage_dir, f"{uuid.uuid4().hex}.pdf")

    size = 0
    first = True
    try:
        with open(storage_path, "wb") as out:
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
            reader = PdfReader(storage_path)
            pages = len(reader.pages)
        except PdfReadError as exc:
            raise InvalidPdfError(f"corrupt PDF: {exc}") from exc

        if pages == 0:
            raise InvalidPdfError("PDF has no pages")
        if pages > max_pages:
            raise InvalidPdfError(f"PDF exceeds {max_pages} pages")

        return storage_path, size, pages
    except Exception:
        # Fail-closed: never leave orphan files from a rejected upload.
        try:
            os.remove(storage_path)
        except OSError:
            pass
        raise

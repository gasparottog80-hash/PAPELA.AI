from __future__ import annotations

import re

# LGPD: OCR/parse exceptions can embed fragments of the document (extracted
# text, page snippets). We must never persist or log the raw exception string.
# Instead we store a stable, allow-listed category + the exception class name,
# so operators can triage failures without leaking document contents.

_MAX_LEN = 200

# Ordered (pattern -> category). First match wins. Patterns match the LOWERCASED
# exception text and are deliberately generic (no capture of document content).
_CATEGORIES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"password|encrypt|decrypt"), "pdf_encrypted"),
    (re.compile(r"corrupt|damaged|eof|xref|startxref|invalid pdf"), "pdf_corrupt"),
    (re.compile(r"timeout|timed out|deadline"), "ocr_timeout"),
    (re.compile(r"memory|oom|cannot allocate"), "resource_exhausted"),
    (re.compile(r"no such file|not found|no pages|empty"), "pdf_unreadable"),
    (
        re.compile(r"connection|refused|reset|unreachable|pool"),
        "dependency_unavailable",
    ),
]


def sanitize_error(exc: BaseException) -> str:
    """Return an LGPD-safe error string: `<Category>: <ExcClassName>`.

    Never includes the exception's message text, which may contain document
    content. Falls back to a generic category when nothing matches.
    """
    exc_name = type(exc).__name__
    text = str(exc).lower()
    category = "processing_error"
    for pattern, name in _CATEGORIES:
        if pattern.search(text):
            category = name
            break
    return f"{category}: {exc_name}"[:_MAX_LEN]

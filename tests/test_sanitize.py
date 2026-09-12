from __future__ import annotations

from app.sanitize import sanitize_error


def test_sanitize_never_leaks_document_content():
    # SUCCESS + CRITICAL EDGE: an exception carrying extracted document text
    # must NOT appear in the sanitized output (LGPD).
    secret = "CNPJ 12.345.678/0001-99 valor R$ 9.999,00 cliente Fulano"
    out = sanitize_error(ValueError(f"failed parsing page: {secret}"))
    assert secret not in out
    assert "9.999" not in out
    assert "ValueError" in out  # class name is safe to keep


def test_sanitize_categorizes_known_failures():
    assert sanitize_error(ValueError("PDF is encrypted, password required")).startswith(
        "pdf_encrypted:"
    )
    assert sanitize_error(RuntimeError("xref table corrupt")).startswith("pdf_corrupt:")
    assert sanitize_error(TimeoutError("OCR timed out after 30s")).startswith(
        "ocr_timeout:"
    )


def test_sanitize_unknown_falls_back_to_generic():
    # EXPECTED non-match: unknown error still yields a safe, bounded string.
    out = sanitize_error(Exception("weird internal thing 0xdeadbeef"))
    assert out == "processing_error: Exception"
    assert "0xdeadbeef" not in out
    assert len(out) <= 200

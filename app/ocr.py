from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger("papela.ocr")


class OcrEngine(Protocol):
    """Contract for OCR backends. Keeps PaddleOCR (500MB models, native libs)
    out of the API process and out of the test path."""

    def extract(self, pdf_path: str) -> dict[str, Any]: ...


class FakeOcrEngine:
    """Deterministic engine for dev/CI. No model download, no native deps."""

    def extract(self, pdf_path: str) -> dict[str, Any]:
        from pypdf import PdfReader

        reader = PdfReader(pdf_path)
        pages = []
        for i, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            pages.append({"page": i + 1, "text": text, "confidence": 1.0})
        return {"engine": "fake", "page_count": len(pages), "pages": pages}


class PaddleOcrEngine:
    """Real on-premise OCR. Requires the `ocr` extra + rasterizes pages via
    PyMuPDF. Lazily constructed so importing this module stays cheap."""

    def __init__(self, lang: str = "pt") -> None:
        from paddleocr import PaddleOCR  # heavy import, deferred

        self._ocr = PaddleOCR(use_angle_cls=True, lang=lang, show_log=False)

    def extract(self, pdf_path: str) -> dict[str, Any]:
        import fitz  # PyMuPDF
        import numpy as np

        doc = fitz.open(pdf_path)
        pages: list[dict[str, Any]] = []
        try:
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=200)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width, pix.n
                )
                result = self._ocr.ocr(img, cls=True)
                lines: list[str] = []
                confs: list[float] = []
                for block in result or []:
                    for _box, (txt, conf) in block or []:
                        lines.append(txt)
                        confs.append(float(conf))
                pages.append(
                    {
                        "page": i + 1,
                        "text": "\n".join(lines),
                        "confidence": (sum(confs) / len(confs)) if confs else 0.0,
                    }
                )
        finally:
            doc.close()
        return {"engine": "paddle", "page_count": len(pages), "pages": pages}


def build_engine(name: str, lang: str) -> OcrEngine:
    if name == "paddle":
        return PaddleOcrEngine(lang=lang)
    if name == "fake":
        return FakeOcrEngine()
    raise ValueError(f"unknown ocr_engine: {name!r}")

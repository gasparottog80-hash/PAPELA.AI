from __future__ import annotations

import logging
from typing import Any, Protocol

from .tables import parse_table_html

logger = logging.getLogger("papela.ocr")

# Result schema version. v2 adds per-page `tables` (structured rows + source
# HTML) alongside the v1 `text`/`confidence`. v3 adds a top-level `fields`
# object (deterministic fiscal extraction, added by the worker AFTER OCR — the
# engines still emit only the OCR payload). Bump when the shape changes so
# stored results in Postgres can be migrated/interpreted deterministically.
SCHEMA_VERSION = 3

# Deterministic sample table used by the fake engine so the full structured
# pipeline (parse -> worker -> Postgres JSONB -> API) is exercised without
# downloading any model. Runs through the SAME parser as real output.
_FAKE_TABLE_HTML = (
    "<table><thead><tr><th>Item</th><th>Valor</th></tr></thead>"
    "<tbody><tr><td>Servico A</td><td>100,00</td></tr>"
    "<tr><td>Servico B</td><td>250,50</td></tr></tbody></table>"
)


class OcrEngine(Protocol):
    """Contract for OCR backends. Keeps PaddleOCR (500MB models, native libs)
    out of the API process and out of the test path.

    `extract` returns a schema-v2 dict:
        {
          "engine": str,
          "schema_version": int,
          "page_count": int,
          "pages": [
            {
              "page": int,
              "text": str,
              "confidence": float,
              "tables": [
                {"table_id","bbox","n_rows","n_cols","rows","html"}
              ]
            }
          ]
        }
    """

    def extract(self, pdf_path: str) -> dict[str, Any]: ...


def _empty_bbox() -> list[int]:
    return [0, 0, 0, 0]


class FakeOcrEngine:
    """Deterministic engine for dev/CI. No model download, no native deps.

    Emits the full v2 shape, including one structured table on page 1, so tests
    validate the structured path end to end.
    """

    def extract(self, pdf_path: str) -> dict[str, Any]:
        from pypdf import PdfReader

        reader = PdfReader(pdf_path)
        pages: list[dict[str, Any]] = []
        for i, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            tables: list[dict[str, Any]] = []
            if i == 0:  # attach a deterministic table to the first page
                parsed = parse_table_html(_FAKE_TABLE_HTML)
                tables.append(
                    {
                        "table_id": 0,
                        "bbox": _empty_bbox(),
                        "n_rows": parsed["n_rows"],
                        "n_cols": parsed["n_cols"],
                        "rows": parsed["rows"],
                        "html": _FAKE_TABLE_HTML,
                    }
                )
            pages.append(
                {"page": i + 1, "text": text, "confidence": 1.0, "tables": tables}
            )
        return {
            "engine": "fake",
            "schema_version": SCHEMA_VERSION,
            "page_count": len(pages),
            "pages": pages,
        }


def _pdf_to_bgr_images(pdf_path: str, dpi: int = 200):
    """Yield (page_index, BGR numpy image) for each PDF page. PaddleOCR expects
    BGR; PyMuPDF renders RGB(A), so we drop alpha and flip channels."""
    import fitz  # PyMuPDF
    import numpy as np

    doc = fitz.open(pdf_path)
    try:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=dpi)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n
            )
            if img.shape[2] == 4:  # RGBA -> RGB
                img = img[:, :, :3]
            img = img[:, :, ::-1]  # RGB -> BGR
            yield i, np.ascontiguousarray(img)
    finally:
        doc.close()


class PaddleOcrEngine:
    """Plain on-premise OCR (text only). Kept for back-compat; emits the v2
    shape with empty `tables`. Use `ppstructure` for tables."""

    def __init__(self, lang: str = "pt") -> None:
        from paddleocr import PaddleOCR  # heavy import, deferred

        self._ocr = PaddleOCR(use_angle_cls=True, lang=lang, show_log=False)

    def extract(self, pdf_path: str) -> dict[str, Any]:
        pages: list[dict[str, Any]] = []
        for i, img in _pdf_to_bgr_images(pdf_path):
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
                    "tables": [],
                }
            )
        return {
            "engine": "paddle",
            "schema_version": SCHEMA_VERSION,
            "page_count": len(pages),
            "pages": pages,
        }


def _region_sort_key(region: dict[str, Any]) -> tuple[int, int]:
    bbox = region.get("bbox") or [0, 0, 0, 0]
    try:
        return int(bbox[1]), int(bbox[0])  # reading order: top-to-bottom, left-to-right
    except (TypeError, ValueError, IndexError):
        return 0, 0


def _to_bbox(bbox: Any) -> list[int]:
    try:
        return [int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])]
    except (TypeError, ValueError, IndexError):
        return _empty_bbox()


class PPStructureEngine:
    """On-premise layout analysis + table recognition via PaddleOCR PP-Structure.

    Returns text AND structured tables (2D rows parsed from the model's table
    HTML, plus the lossless HTML). 100% local — no external API. Requires the
    `ocr` extra (paddleocr 2.x, paddlepaddle, pymupdf).
    """

    def __init__(self, lang: str = "pt") -> None:
        from paddleocr import PPStructure  # heavy import, deferred

        # Table structure recognition is language-agnostic; `lang` selects the
        # text recognizer. layout+table+ocr all on, all local.
        self._engine = PPStructure(
            show_log=False, lang=lang, layout=True, table=True, ocr=True
        )

    def extract(self, pdf_path: str) -> dict[str, Any]:
        pages: list[dict[str, Any]] = []
        for i, img in _pdf_to_bgr_images(pdf_path):
            regions = self._engine(img) or []
            text_parts: list[str] = []
            confs: list[float] = []
            tables: list[dict[str, Any]] = []
            for region in sorted(regions, key=_region_sort_key):
                rtype = str(region.get("type", "")).lower()
                res = region.get("res")
                if rtype == "table":
                    html = ""
                    if isinstance(res, dict):
                        html = res.get("html", "") or ""
                    parsed = parse_table_html(html)
                    tables.append(
                        {
                            "table_id": len(tables),
                            "bbox": _to_bbox(region.get("bbox")),
                            "n_rows": parsed["n_rows"],
                            "n_cols": parsed["n_cols"],
                            "rows": parsed["rows"],
                            "html": html,
                        }
                    )
                elif isinstance(res, list):  # text/title/list/header/footer
                    for line in res:
                        if isinstance(line, dict) and line.get("text"):
                            text_parts.append(str(line["text"]))
                            if line.get("confidence") is not None:
                                confs.append(float(line["confidence"]))
            pages.append(
                {
                    "page": i + 1,
                    "text": "\n".join(text_parts),
                    "confidence": (sum(confs) / len(confs)) if confs else 0.0,
                    "tables": tables,
                }
            )
        return {
            "engine": "ppstructure",
            "schema_version": SCHEMA_VERSION,
            "page_count": len(pages),
            "pages": pages,
        }


def build_engine(name: str, lang: str) -> OcrEngine:
    if name == "ppstructure":
        return PPStructureEngine(lang=lang)
    if name == "paddle":
        return PaddleOcrEngine(lang=lang)
    if name == "fake":
        return FakeOcrEngine()
    raise ValueError(f"unknown ocr_engine: {name!r}")

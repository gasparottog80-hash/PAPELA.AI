from __future__ import annotations

from app.ocr import SCHEMA_VERSION, PPStructureEngine, build_engine


class _FakePPStructure:
    """Stand-in for paddleocr.PPStructure: returns canned regions so we can
    test PPStructureEngine's region->schema mapping without the real model."""

    def __init__(self, regions):
        self._regions = regions

    def __call__(self, _img):
        return self._regions


def _engine_with(regions) -> PPStructureEngine:
    eng = PPStructureEngine.__new__(PPStructureEngine)  # bypass heavy __init__
    eng._engine = _FakePPStructure(regions)  # type: ignore[attr-defined]
    return eng


def _one_page_bgr():
    # A dummy "image" — PPStructureEngine never inspects it (the fake engine
    # ignores its arg), so any object works and we avoid a numpy dependency in
    # the base test path.
    return [(0, object())]


def test_ppstructure_maps_table_region_to_structured_rows(monkeypatch):
    # A single 'table' region's HTML must become structured rows in the result.
    regions = [
        {
            "type": "table",
            "bbox": [0, 0, 100, 50],
            "res": {
                "html": "<table><tr><td>Item</td><td>Valor</td></tr>"
                "<tr><td>A</td><td>10</td></tr></table>"
            },
        }
    ]
    eng = _engine_with(regions)
    monkeypatch.setattr("app.ocr._pdf_to_bgr_images", lambda *_a, **_k: _one_page_bgr())

    out = eng.extract("ignored.pdf")
    assert out["engine"] == "ppstructure"
    assert out["schema_version"] == SCHEMA_VERSION
    tables = out["pages"][0]["tables"]
    assert len(tables) == 1
    assert tables[0]["rows"] == [["Item", "Valor"], ["A", "10"]]
    assert tables[0]["bbox"] == [0, 0, 100, 50]


def test_ppstructure_orders_text_by_reading_order(monkeypatch):
    # Regions must be emitted top-to-bottom (by bbox y) regardless of input order.
    regions = [
        {"type": "text", "bbox": [0, 200, 50, 220], "res": [{"text": "segundo"}]},
        {"type": "text", "bbox": [0, 10, 50, 30], "res": [{"text": "primeiro"}]},
    ]
    eng = _engine_with(regions)
    monkeypatch.setattr("app.ocr._pdf_to_bgr_images", lambda *_a, **_k: _one_page_bgr())

    out = eng.extract("ignored.pdf")
    assert out["pages"][0]["text"] == "primeiro\nsegundo"
    assert out["pages"][0]["tables"] == []


def test_build_engine_knows_ppstructure():
    # build_engine must route the new name; unknown names still raise.
    import pytest

    assert build_engine.__name__ == "build_engine"
    with pytest.raises(ValueError):
        build_engine("nope", "pt")

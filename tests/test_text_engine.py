from __future__ import annotations

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.ocr import TextLayerEngine


def test_text_layer_engine_extracts_embedded_text_without_ocr(tmp_path):
    path = tmp_path / "digital.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 25 100 Td (Invoice 123) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as output:
        writer.write(output)

    result = TextLayerEngine().extract(str(path))
    assert result["engine"] == "text"
    assert result["pages"][0]["text"] == "Invoice 123"
    assert result["pages"][0]["tables"] == []


def test_text_layer_engine_rejects_scanned_or_blank_pdf(tmp_path):
    path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as output:
        writer.write(output)
    with pytest.raises(ValueError, match="text layer unavailable"):
        TextLayerEngine().extract(str(path))

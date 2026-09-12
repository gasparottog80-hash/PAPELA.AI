from __future__ import annotations

from app.tables import parse_table_html


def test_parse_simple_grid():
    # SUCCESS: a clean 2x2 (+header) table becomes a rectangular grid.
    html = (
        "<table><tr><th>Item</th><th>Valor</th></tr>"
        "<tr><td>A</td><td>100,00</td></tr>"
        "<tr><td>B</td><td>250,50</td></tr></table>"
    )
    out = parse_table_html(html)
    assert out["n_rows"] == 3
    assert out["n_cols"] == 2
    assert out["rows"] == [
        ["Item", "Valor"],
        ["A", "100,00"],
        ["B", "250,50"],
    ]


def test_parse_colspan_expands_and_keeps_rectangular():
    # CRITICAL EDGE: colspan must expand so every row has n_cols cells; only the
    # top-left of the span carries text.
    html = (
        "<table><tr><td colspan='2'>Total</td></tr>"
        "<tr><td>x</td><td>y</td></tr></table>"
    )
    out = parse_table_html(html)
    assert out["n_cols"] == 2
    assert out["rows"] == [["Total", ""], ["x", "y"]]


def test_parse_rowspan_expands_down():
    # CRITICAL EDGE: rowspan pushes the anchor cell's column down; the cell to
    # its right on the second row must land in the correct column.
    html = (
        "<table><tr><td rowspan='2'>A</td><td>b1</td></tr>"
        "<tr><td>b2</td></tr></table>"
    )
    out = parse_table_html(html)
    assert out["n_rows"] == 2
    assert out["n_cols"] == 2
    assert out["rows"] == [["A", "b1"], ["", "b2"]]


def test_parse_malformed_returns_empty_not_raise():
    # EXPECTED FAILURE mode: garbage / non-table input yields an empty grid,
    # never an exception (a bad table must not fail the OCR job).
    assert parse_table_html("not html at all") == {"n_rows": 0, "n_cols": 0, "rows": []}
    assert parse_table_html("") == {"n_rows": 0, "n_cols": 0, "rows": []}
    assert parse_table_html("<table></table>") == {
        "n_rows": 0,
        "n_cols": 0,
        "rows": [],
    }

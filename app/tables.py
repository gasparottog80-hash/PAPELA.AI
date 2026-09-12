from __future__ import annotations

from html.parser import HTMLParser


def _to_int(value: str | None, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class _TableHTMLParser(HTMLParser):
    """Collect (text, colspan, rowspan) per cell, grouped by row.

    Tolerant by design: PP-Structure table HTML is machine-generated and mostly
    well-formed, but we never want a malformed table to crash a whole job.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, int, int]]] = []
        self._row: list[tuple[str, int, int]] | None = None
        self._in_cell = False
        self._text: list[str] = []
        self._span = (1, 1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            if self._row is None:  # <td> outside <tr>: start an implicit row
                self._row = []
            self._in_cell = True
            self._text = []
            a = {k.lower(): v for k, v in attrs}
            self._span = (_to_int(a.get("colspan"), 1), _to_int(a.get("rowspan"), 1))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("td", "th") and self._in_cell:
            text = " ".join("".join(self._text).split())
            cs, rs = self._span
            assert self._row is not None
            self._row.append((text, cs, rs))
            self._in_cell = False
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._text.append(data)


def parse_table_html(html: str) -> dict[str, object]:
    """Convert a PP-Structure `<table>` HTML string into a rectangular grid.

    Returns ``{"n_rows", "n_cols", "rows"}`` where ``rows`` is a list of equal
    length string lists. colspan/rowspan are expanded onto the grid; only the
    top-left cell of a span carries the text, the rest are empty strings (clean
    for downstream field extraction). Malformed/empty input returns an empty
    grid instead of raising — a bad table must never fail the OCR job.
    """
    empty: dict[str, object] = {"n_rows": 0, "n_cols": 0, "rows": []}
    if not html or "<" not in html:
        return empty

    parser = _TableHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - tolerate any HTML pathology
        return empty

    raw_rows = parser.rows
    if not raw_rows:
        return empty

    # Fill an (row, col) -> text map, honoring spans. rowspans can push the grid
    # beyond len(raw_rows), so derive final dimensions from the filled cells.
    occupied: dict[tuple[int, int], str] = {}
    for r, cells in enumerate(raw_rows):
        c = 0
        for text, cs, rs in cells:
            while (r, c) in occupied:
                c += 1
            cs = max(1, cs)
            rs = max(1, rs)
            for dr in range(rs):
                for dc in range(cs):
                    occupied[(r + dr, c + dc)] = text if (dr == 0 and dc == 0) else ""
            c += cs

    if not occupied:
        return empty

    n_rows = max(r for r, _ in occupied) + 1
    n_cols = max(c for _, c in occupied) + 1
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for (r, c), text in occupied.items():
        grid[r][c] = text
    return {"n_rows": n_rows, "n_cols": n_cols, "rows": grid}

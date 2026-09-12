from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional, TypeVar

from .sanitize import sanitize_error

logger = logging.getLogger("papela.extract")

# Deterministic fiscal-field extraction over the OCR envelope (text + structured
# tables). No LLM, no external calls — regex + heuristics + check-digit
# validation. LGPD-friendly: runs entirely on the already-local OCR result.
#
# Design contract:
# - Pure, side-effect-free, and TOLERANT: it must never raise into the worker
#   (a broken extraction can't be allowed to fail an otherwise-good OCR job).
#   `extract_fields` wraps every sub-extractor and degrades to null/empty.
# - Money and quantities are emitted as STRINGS (not float/Decimal) so the
#   result stays exact and JSON/JSONB-serializable.
# - Conservative on ambiguity: emitente/destinatario roles are only asserted
#   from an explicit label or a well-defined positional fallback, each tagged
#   with `role_source` so consumers know how a role was decided.
EXTRACTOR_VERSION = 1


# --------------------------------------------------------------------------- #
# Validators (check digits kill the bulk of regex false positives)
# --------------------------------------------------------------------------- #
def _only_digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def valid_cnpj(digits: str) -> bool:
    """Brazilian CNPJ check-digit (mod 11) validation. `digits` = 14 chars."""
    if len(digits) != 14 or len(set(digits)) == 1:
        return False

    def _dv(nums: str, weights: list[int]) -> str:
        total = sum(int(n) * w for n, w in zip(nums, weights))
        rem = total % 11
        return "0" if rem < 2 else str(11 - rem)

    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    w2 = [6] + w1
    d1 = _dv(digits[:12], w1)
    d2 = _dv(digits[:12] + d1, w2)
    return digits[12] == d1 and digits[13] == d2


def valid_cpf(digits: str) -> bool:
    """Brazilian CPF check-digit (mod 11) validation. `digits` = 11 chars."""
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    s = sum(int(digits[i]) * (10 - i) for i in range(9))
    r = (s * 10) % 11
    d1 = 0 if r == 10 else r
    if d1 != int(digits[9]):
        return False
    s = sum(int(digits[i]) * (11 - i) for i in range(10))
    r = (s * 10) % 11
    d2 = 0 if r == 10 else r
    return d2 == int(digits[10])


def _format_cnpj(d: str) -> str:
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def _format_cpf(d: str) -> str:
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


# --------------------------------------------------------------------------- #
# Brazilian number / currency parsing
# --------------------------------------------------------------------------- #
def parse_br_number(raw: str) -> Optional[Decimal]:
    """Parse a pt-BR numeric string ('1.234,56', '100,00', '2') to Decimal.

    Thousands separator is '.', decimal separator is ','. Returns None on
    anything unparseable.
    """
    s = raw.strip()
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".") if "," in s else s.replace(".", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _money_str(d: Decimal) -> str:
    return f"{d:.2f}"


def _qty_str(d: Decimal) -> str:
    # Drop insignificant trailing zeros ('2.000' -> '2', '1.50' -> '1.5').
    normalized = d.normalize()
    return format(normalized, "f")


# --------------------------------------------------------------------------- #
# Regexes
# --------------------------------------------------------------------------- #
# Bounded by non-digit lookarounds so a CNPJ/CPF is never matched *inside* the
# 44-digit NFe access key (chave de acesso).
_CNPJ_FMT_RE = re.compile(r"(?<!\d)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)")
_CNPJ_BARE_RE = re.compile(r"(?<!\d)\d{14}(?!\d)")
_CPF_FMT_RE = re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)")
_CPF_BARE_RE = re.compile(r"(?<!\d)\d{11}(?!\d)")

# pt-BR currency: '1.234.567,89' | '12345,67' | '100,00'. Requires ,dd cents so
# it doesn't swallow arbitrary integers (phone numbers, IDs, quantities).
_MONEY_RE = re.compile(r"(?<!\d)(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}(?!\d)")

# DD/MM/YYYY or DD/MM/YY.
_DATE_RE = re.compile(r"(?<!\d)(\d{2})/(\d{2})/(\d{2,4})(?!\d)")

# Number token for NF: either dot-grouped (001.234.561) or a plain run of up to
# 9 digits (NFe numbers are <= 9 digits). Capped + boundary-guarded so it can
# NEVER match inside the 44-digit access key. Leading zeros preserved.
_NF_NUM_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d{3}){1,3}|\d{1,9})(?!\d)")

# Role keywords for CNPJ attribution.
_ROLE_KEYWORDS = {
    "destinatario": ("destinat", "tomador"),
    "emitente": ("emitent", "prestador", "remetente"),
}
_ROLE_WINDOW = 200  # chars to look back from a CNPJ match for a role keyword


# --------------------------------------------------------------------------- #
# Text assembly
# --------------------------------------------------------------------------- #
def _document_text(ocr_result: dict[str, Any]) -> str:
    parts: list[str] = []
    for page in ocr_result.get("pages", []) or []:
        txt = page.get("text")
        if txt:
            parts.append(str(txt))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# CNPJ / CPF
# --------------------------------------------------------------------------- #
def _role_for_position(text_lower: str, pos: int) -> tuple[str, Optional[str]]:
    """Nearest role keyword in the window before `pos`. Closest keyword wins."""
    lo = max(0, pos - _ROLE_WINDOW)
    window = text_lower[lo:pos]
    best_role = "unknown"
    best_idx = -1
    for role, kws in _ROLE_KEYWORDS.items():
        for kw in kws:
            idx = window.rfind(kw)
            if idx > best_idx:
                best_idx = idx
                best_role = role
    return (best_role, "label") if best_idx >= 0 else ("unknown", None)


def _extract_cnpjs(text: str) -> list[dict[str, Any]]:
    text_lower = text.lower()
    found: dict[str, dict[str, Any]] = {}  # digits -> record (first occurrence)
    order: list[str] = []

    def _consider(match_text: str, pos: int, from_formatted: bool) -> None:
        digits = _only_digits(match_text)
        is_valid = valid_cnpj(digits)
        # Bare 14-digit runs are noisy; only trust them if the check digit holds.
        if not from_formatted and not is_valid:
            return
        if digits in found:
            return
        role, role_source = _role_for_position(text_lower, pos)
        found[digits] = {
            "value": _format_cnpj(digits),
            "digits": digits,
            "valid": is_valid,
            "role": role,
            "role_source": role_source,
        }
        order.append(digits)

    for m in _CNPJ_FMT_RE.finditer(text):
        _consider(m.group(0), m.start(), from_formatted=True)
    for m in _CNPJ_BARE_RE.finditer(text):
        _consider(m.group(0), m.start(), from_formatted=False)

    cnpjs = [found[d] for d in order]

    # Positional fallback: exactly two unlabeled valid CNPJs in a DANFE almost
    # always are (emitente, destinatario) in that order. Tag as 'position' so
    # consumers can distinguish it from a label-derived role.
    labeled = [c for c in cnpjs if c["role_source"] == "label"]
    valid_unlabeled = [
        c for c in cnpjs if c["valid"] and c["role_source"] is None
    ]
    if not labeled and len(valid_unlabeled) == 2:
        valid_unlabeled[0]["role"] = "emitente"
        valid_unlabeled[0]["role_source"] = "position"
        valid_unlabeled[1]["role"] = "destinatario"
        valid_unlabeled[1]["role_source"] = "position"
    return cnpjs


def _extract_cpfs(text: str) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for regex, from_formatted in ((_CPF_FMT_RE, True), (_CPF_BARE_RE, False)):
        for m in regex.finditer(text):
            digits = _only_digits(m.group(0))
            is_valid = valid_cpf(digits)
            if not from_formatted and not is_valid:
                continue
            if digits in found:
                continue
            found[digits] = {
                "value": _format_cpf(digits),
                "digits": digits,
                "valid": is_valid,
            }
            order.append(digits)
    return [found[d] for d in order]


def _pick_role(cnpjs: list[dict[str, Any]], role: str) -> Optional[dict[str, Any]]:
    # Prefer a valid, label-sourced CNPJ; fall back to any with the role.
    candidates = [c for c in cnpjs if c["role"] == role]
    for c in candidates:
        if c["valid"] and c["role_source"] == "label":
            return c
    return candidates[0] if candidates else None


# --------------------------------------------------------------------------- #
# Labeled scalar fields (NF number, date, total)
# --------------------------------------------------------------------------- #
def _search_labeled(
    text: str,
    text_lower: str,
    label_res: list[str],
    value_re: re.Pattern[str],
    window: int,
) -> Optional[tuple[re.Match[str], str]]:
    """Find the first value matching `value_re` within `window` chars after any
    label (labels tried in priority order). Returns (value_match, label).

    Searches the FULL text (not a slice) so `value_re`'s digit-boundary
    lookarounds see the real neighbouring characters — slicing would truncate a
    long digit run (e.g. the 44-digit NFe access key) and forge a false
    boundary. Matches are only accepted while they start within `window` chars
    of the label end.
    """
    for label in label_res:
        for lm in re.finditer(label, text_lower):
            start = lm.end()
            for vm in value_re.finditer(text, start):
                if vm.start() - start > window:
                    break  # past the window; this label yields nothing here
                return vm, label
    return None


_NF_LABELS = [
    r"n[ºo°\.]?\s*da\s*nota",
    r"n[uú]mero\s*da\s*nota",
    r"nota\s+fiscal(?:\s+eletr[oô]nica)?\s*n?[ºo°\.]?",
    r"nf-?e\s*n?[ºo°\.]?",
    r"nfs-?e\s*n?[ºo°\.]?",
    r"nfc-?e\s*n?[ºo°\.]?",
    # Bare "numero" fallback: excludes "numero de/da/do <coisa>" (numero de
    # controle, do pedido, da guia...) — those are non-fiscal reference
    # numbers, not the NF number. "numero da nota" itself is already matched
    # by the more specific, higher-priority label above.
    r"n[uú]mero(?!\s+(?:de|da|do)\b)",
    r"n[ºo°]\.?",
]

_DATE_LABELS = [
    r"data\s+de?\s+emiss[aã]o",
    r"data\s+da\s+emiss[aã]o",
    r"emiss[aã]o",
    r"data\s+de?\s+sa[ií]da",
]

_TOTAL_LABELS = [
    r"valor\s+total\s+da\s+nota",
    r"valor\s+total\s+dos?\s+servi[cç]os?",
    r"valor\s+total\s+dos?\s+produtos?",
    r"valor\s+l[ií]quido",
    r"valor\s+total",
    r"total\s+da\s+nota",
    r"total\s+geral",
    r"total\s+a\s+pagar",
    r"vlr?\.?\s*total",
    r"total\s*r?\$",
]


def _extract_numero_nf(text: str, text_lower: str) -> Optional[dict[str, Any]]:
    hit = _search_labeled(text, text_lower, _NF_LABELS, _NF_NUM_RE, window=24)
    if hit is None:
        return None
    vm, label = hit
    # Dot-grouped numbers (001.234.561) -> digits only, leading zeros kept.
    value = vm.group(1).replace(".", "")
    return {"value": value, "label": label}


def _parse_date(dm: re.Match[str]) -> Optional[str]:
    day, month, year = dm.group(1), dm.group(2), dm.group(3)
    fmt = "%d/%m/%Y" if len(year) == 4 else "%d/%m/%y"
    try:
        parsed = datetime.strptime(f"{day}/{month}/{year}", fmt).date()
    except ValueError:
        return None
    return parsed.isoformat()


def _extract_data_emissao(text: str, text_lower: str) -> Optional[dict[str, Any]]:
    hit = _search_labeled(text, text_lower, _DATE_LABELS, _DATE_RE, window=24)
    if hit is not None:
        vm, _label = hit
        iso = _parse_date(vm)
        if iso:
            return {"value": iso, "raw": vm.group(0), "source": "label"}
    # Fallback: first valid date anywhere in the document.
    for vm in _DATE_RE.finditer(text):
        iso = _parse_date(vm)
        if iso:
            return {"value": iso, "raw": vm.group(0), "source": "first_date"}
    return None


def _extract_valor_total(text: str, text_lower: str) -> Optional[dict[str, Any]]:
    hit = _search_labeled(text, text_lower, _TOTAL_LABELS, _MONEY_RE, window=40)
    if hit is None:
        return None
    vm, label = hit
    dec = parse_br_number(vm.group(0))
    if dec is None:
        return None
    return {"value": _money_str(dec), "raw": vm.group(0), "label": label}


# --------------------------------------------------------------------------- #
# Line items (from structured tables)
# --------------------------------------------------------------------------- #
def _classify_header(header: str) -> Optional[str]:
    h = header.lower().strip()
    if not h:
        return None
    if any(k in h for k in ("descri", "produto", "servi", "discrimina", "mercadoria")):
        return "descricao"
    if "unit" in h and any(k in h for k in ("val", "vl", "pre")):
        return "valor_unitario"
    if ("total" in h or "val" in h or "vl" in h) and "unit" not in h:
        return "valor_total"
    if any(k in h for k in ("qtd", "quant", "qtde")):
        return "quantidade"
    if any(k in h for k in ("c\u00f3d", "cod", "ncm")):
        return "codigo"
    if h in ("item", "itens", "descri\u00e7\u00e3o", "descricao"):
        return "descricao"
    return None


_MONEY_FIELDS = {"valor_unitario", "valor_total"}


def _extract_itens(ocr_result: dict[str, Any]) -> list[dict[str, Any]]:
    itens: list[dict[str, Any]] = []
    for page in ocr_result.get("pages", []) or []:
        page_no = page.get("page")
        for table in page.get("tables", []) or []:
            rows = table.get("rows") or []
            if len(rows) < 2:
                continue
            header = rows[0]
            col_map: dict[int, str] = {}
            for idx, cell in enumerate(header):
                canonical = _classify_header(str(cell))
                # First column wins a given canonical name (avoid double-map).
                if canonical and canonical not in col_map.values():
                    col_map[idx] = canonical
            # Require a description column or >=2 recognized columns to trust it.
            has_desc = "descricao" in col_map.values()
            if not has_desc and len(col_map) < 2:
                continue
            for row in rows[1:]:
                if not any(str(c).strip() for c in row):
                    continue  # skip blank rows
                item: dict[str, Any] = {
                    "page": page_no,
                    "table_id": table.get("table_id"),
                    "raw": list(row),
                }
                for idx, canonical in col_map.items():
                    if idx >= len(row):
                        continue
                    raw_val = str(row[idx]).strip()
                    if canonical in _MONEY_FIELDS:
                        dec = parse_br_number(raw_val)
                        item[canonical] = _money_str(dec) if dec is not None else None
                    elif canonical == "quantidade":
                        dec = parse_br_number(raw_val)
                        item[canonical] = _qty_str(dec) if dec is not None else raw_val
                    else:
                        item[canonical] = raw_val
                itens.append(item)
    return itens


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
_T = TypeVar("_T")


def _safe(fn: Callable[..., _T], default: _T, *args: Any) -> _T:
    """Run a sub-extractor, swallowing any failure so extraction can never fail
    an otherwise-good OCR job.

    LGPD: logs only the sanitized error category + the failing extractor name.
    NEVER uses logger.exception / exc_info — a traceback embeds the exception
    message and local frames, which can carry document content (PII / fiscal
    data). Mirrors the worker's PII-safe error policy (app/sanitize.py).
    """
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - extraction must never fail the OCR job
        logger.error(
            "extract.field_failed",
            extra={
                "request_id": None,
                "extractor": getattr(fn, "__name__", "?"),
                "error": sanitize_error(exc),
            },
        )
        return default


def extract_fields(ocr_result: dict[str, Any]) -> dict[str, Any]:
    """Extract fiscal fields from a schema-v3 OCR envelope.

    Never raises: any sub-extractor failure degrades to null/empty and is
    logged (without document content). Returns a dict:

        {
          "extractor_version": int,
          "emitente_cnpj": {value,digits,valid,role,role_source} | None,
          "destinatario_cnpj": {...} | None,
          "cnpjs": [ {...}, ... ],
          "cpfs":  [ {value,digits,valid}, ... ],
          "numero_nf": {value, label} | None,
          "data_emissao": {value(ISO), raw, source} | None,
          "valor_total": {value(str), raw, label} | None,
          "itens": [ {page, table_id, descricao?, quantidade?,
                      valor_unitario?, valor_total?, codigo?, raw}, ... ]
        }
    """
    text = _safe(_document_text, "", ocr_result)
    text_lower = text.lower()

    cnpjs = _safe(_extract_cnpjs, [], text)
    fields: dict[str, Any] = {
        "extractor_version": EXTRACTOR_VERSION,
        "cnpjs": cnpjs,
        "emitente_cnpj": _safe(_pick_role, None, cnpjs, "emitente"),
        "destinatario_cnpj": _safe(_pick_role, None, cnpjs, "destinatario"),
        "cpfs": _safe(_extract_cpfs, [], text),
        "numero_nf": _safe(_extract_numero_nf, None, text, text_lower),
        "data_emissao": _safe(_extract_data_emissao, None, text, text_lower),
        "valor_total": _safe(_extract_valor_total, None, text, text_lower),
        "itens": _safe(_extract_itens, [], ocr_result),
    }
    return fields

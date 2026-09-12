from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from app.extract import extract_fields

# Deterministic, reproducible accuracy measurement for app/extract.py against a
# fixed set of synthetic fixtures under tests/fixtures/extraction/ — no PII, no
# real documents.
#
# Usage: `uv run python tests/evaluate_extraction.py`

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "extraction"

SCALAR_FIELDS = ("numero_nf", "data_emissao", "valor_total")
LIST_FIELDS = ("cnpjs", "cpfs")
ROLE_FIELDS = ("emitente_cnpj", "destinatario_cnpj")
FIELD_ORDER = (*SCALAR_FIELDS, *LIST_FIELDS, *ROLE_FIELDS, "itens")


def discover_fixtures() -> list[str]:
    """Base names of every fixture pair under FIXTURES_DIR, sorted for a
    deterministic evaluation order (independent of filesystem iteration order)."""
    return sorted(
        p.stem for p in FIXTURES_DIR.glob("*.json") if not p.stem.endswith(".expected")
    )


def load_fixture(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    envelope = json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    expected = json.loads(
        (FIXTURES_DIR / f"{name}.expected.json").read_text(encoding="utf-8")
    )
    return envelope, expected


def _scalar_value(field: dict[str, Any] | None) -> Any:
    return field["value"] if field else None


def _role_value(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {"digits": entry["digits"], "role_source": entry["role_source"]}


def normalize_actual(fields: dict[str, Any]) -> dict[str, Any]:
    """Project extract_fields()'s output down to the same shape used by the
    ground-truth (.expected.json) files, so the two sides are directly
    comparable field-by-field."""
    return {
        "numero_nf": _scalar_value(fields.get("numero_nf")),
        "data_emissao": _scalar_value(fields.get("data_emissao")),
        "valor_total": _scalar_value(fields.get("valor_total")),
        "cnpjs": [c["digits"] for c in fields.get("cnpjs", [])],
        "cpfs": [c["digits"] for c in fields.get("cpfs", [])],
        "emitente_cnpj": _role_value(fields.get("emitente_cnpj")),
        "destinatario_cnpj": _role_value(fields.get("destinatario_cnpj")),
        "itens": [
            {k: v for k, v in item.items() if k not in ("page", "table_id", "raw")}
            for item in fields.get("itens", [])
        ],
    }


def _compare_scalar(expected: Any, actual: Any) -> bool:
    return expected == actual


def _compare_list(expected: list[str], actual: list[str]) -> bool:
    # Order isn't part of the contract for cnpjs/cpfs; set membership is what
    # matters (extra or missing distinct values are real errors).
    return set(expected or []) == set(actual or [])


def _compare_role(expected: dict[str, Any] | None, actual: dict[str, Any] | None) -> bool:
    if expected is None:
        return actual is None
    if actual is None:
        return False
    if expected.get("digits") != actual.get("digits"):
        return False
    # role_source is only checked when the ground truth asserts it, so
    # fixtures that don't care about label-vs-position can omit the key.
    if "role_source" in expected and expected["role_source"] != actual.get("role_source"):
        return False
    return True


def _compare_itens(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> bool:
    # Correct iff row count matches AND every field a row's ground truth
    # declares (descricao/quantidade/valor_unitario/valor_total/codigo) equals
    # the actual row at the same position; rows compare positionally since
    # extract_fields() preserves table row order.
    expected = expected or []
    actual = actual or []
    if len(expected) != len(actual):
        return False
    for exp_item, act_item in zip(expected, actual):
        for key, val in exp_item.items():
            if act_item.get(key) != val:
                return False
    return True


COMPARATORS = {
    "numero_nf": _compare_scalar,
    "data_emissao": _compare_scalar,
    "valor_total": _compare_scalar,
    "cnpjs": _compare_list,
    "cpfs": _compare_list,
    "emitente_cnpj": _compare_role,
    "destinatario_cnpj": _compare_role,
    "itens": _compare_itens,
}


def _minimize(value: Any) -> Any:
    """Shrink a value for safe display in a failure report. Fixtures here are
    100% synthetic, so scalars/digit-lists are safe to print as-is; itens are
    collapsed to a count so a table's contents are never dumped."""
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return f"<{len(value)} itens>"
    return value


def evaluate(names: list[str] | None = None) -> dict[str, Any]:
    """Run extract_fields() over every fixture and score it against ground
    truth. Deterministic: same fixtures in, same result out, every time."""
    names = discover_fixtures() if names is None else names
    field_stats: dict[str, dict[str, Any]] = {
        f: {"cases": 0, "correct": 0} for f in FIELD_ORDER
    }
    failures: list[dict[str, Any]] = []

    for name in names:
        envelope, expected = load_fixture(name)
        actual = normalize_actual(extract_fields(envelope))
        for field in FIELD_ORDER:
            exp_val = expected.get(field)
            act_val = actual.get(field)
            ok = COMPARATORS[field](exp_val, act_val)
            field_stats[field]["cases"] += 1
            if ok:
                field_stats[field]["correct"] += 1
            else:
                failures.append(
                    {
                        "fixture": name,
                        "field": field,
                        "expected": _minimize(exp_val),
                        "actual": _minimize(act_val),
                    }
                )

    for stats in field_stats.values():
        stats["accuracy"] = (
            (stats["correct"] / stats["cases"] * 100.0) if stats["cases"] else 0.0
        )

    total_cases = sum(s["cases"] for s in field_stats.values())
    total_correct = sum(s["correct"] for s in field_stats.values())
    overall = (total_correct / total_cases * 100.0) if total_cases else 0.0

    return {
        "fixture_count": len(names),
        "fields": field_stats,
        "overall": overall,
        "failures": failures,
    }


def format_report(result: dict[str, Any]) -> str:
    lines = ["Extraction Evaluation", "=====================", ""]
    lines.append(f"Fixtures: {result['fixture_count']}")
    lines.append("")
    lines.append(f"{'Field':<18} {'Cases':>6} {'Correct':>8} {'Accuracy':>9}")
    for field in FIELD_ORDER:
        stats = result["fields"][field]
        lines.append(
            f"{field:<18} {stats['cases']:>6} {stats['correct']:>8} "
            f"{stats['accuracy']:>8.1f}%"
        )
    lines.append("")
    lines.append(f"Overall: {result['overall']:.1f}%")

    if result["failures"]:
        lines.append("")
        lines.append("Failures")
        lines.append("--------")
        for fail in result["failures"]:
            lines.append(f"fixture={fail['fixture']}")
            lines.append(f"field={fail['field']}")
            lines.append(f"expected={fail['expected']}")
            lines.append(f"actual={fail['actual']}")
            lines.append("")
    return "\n".join(lines).rstrip("\n")


def main() -> int:
    print(format_report(evaluate()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import json

import evaluate_extraction
from evaluate_extraction import (
    FIELD_ORDER,
    FIXTURES_DIR,
    _compare_itens,
    _compare_list,
    _compare_role,
    _compare_scalar,
    discover_fixtures,
    evaluate,
    load_fixture,
    normalize_actual,
)

from app.extract import extract_fields


# --------------------------------------------------------------------------- #
# Fixture discovery / ground truth sanity
# --------------------------------------------------------------------------- #
def test_discovers_every_fixture_pair():
    names = discover_fixtures()
    assert len(names) >= 12  # the full scenario set this task requires
    for name in names:
        assert (FIXTURES_DIR / f"{name}.json").exists()
        assert (FIXTURES_DIR / f"{name}.expected.json").exists()


def test_all_ground_truths_declare_every_field():
    # Every .expected.json must reflect the extractor's actual contract: no
    # missing/extraneous top-level keys relative to FIELD_ORDER.
    for name in discover_fixtures():
        _, expected = load_fixture(name)
        assert set(expected.keys()) == set(FIELD_ORDER), name


def test_fixtures_are_valid_json_envelopes():
    # Loading must never raise, even for the deliberately malformed envelope.
    for name in discover_fixtures():
        envelope, _ = load_fixture(name)
        assert isinstance(envelope, dict)


# --------------------------------------------------------------------------- #
# Field comparators
# --------------------------------------------------------------------------- #
def test_compare_scalar():
    assert _compare_scalar("123", "123") is True
    assert _compare_scalar(None, None) is True
    assert _compare_scalar("123", "124") is False
    assert _compare_scalar("123", None) is False


def test_compare_list_is_order_independent_set_match():
    assert _compare_list(["a", "b"], ["b", "a"]) is True
    assert _compare_list([], []) is True
    assert _compare_list(["a"], ["a", "b"]) is False  # extra value = error
    assert _compare_list(["a", "b"], ["a"]) is False  # missing value = error


def test_compare_role_checks_digits_and_optional_role_source():
    assert _compare_role(None, None) is True
    assert _compare_role(None, {"digits": "1", "role_source": "label"}) is False
    assert _compare_role({"digits": "1", "role_source": "label"}, None) is False
    assert (
        _compare_role({"digits": "1", "role_source": "label"}, {"digits": "1", "role_source": "label"})
        is True
    )
    assert (
        _compare_role({"digits": "1", "role_source": "label"}, {"digits": "1", "role_source": "position"})
        is False
    )
    # Ground truth omitting role_source means "don't care about it".
    assert _compare_role({"digits": "1"}, {"digits": "1", "role_source": "position"}) is True
    assert _compare_role({"digits": "1"}, {"digits": "2", "role_source": "position"}) is False


def test_compare_itens_requires_matching_length_and_declared_fields():
    exp = [{"descricao": "A", "valor_total": "10.00"}]
    assert _compare_itens(exp, [{"descricao": "A", "valor_total": "10.00", "raw": ["x"]}]) is True
    assert _compare_itens(exp, [{"descricao": "B", "valor_total": "10.00"}]) is False
    assert _compare_itens(exp, []) is False
    assert _compare_itens([], []) is True


# --------------------------------------------------------------------------- #
# normalize_actual() — missing fields must degrade to None/[], never KeyError
# --------------------------------------------------------------------------- #
def test_normalize_actual_handles_missing_fields():
    minimal = extract_fields({})
    normalized = normalize_actual(minimal)
    assert normalized["numero_nf"] is None
    assert normalized["emitente_cnpj"] is None
    assert normalized["cnpjs"] == []
    assert normalized["itens"] == []


def test_normalize_actual_drops_internal_item_bookkeeping():
    fields = {
        "itens": [{"page": 1, "table_id": 0, "raw": ["a", "b"], "descricao": "X"}],
        "cnpjs": [],
        "cpfs": [],
        "emitente_cnpj": None,
        "destinatario_cnpj": None,
        "numero_nf": None,
        "data_emissao": None,
        "valor_total": None,
    }
    normalized = normalize_actual(fields)
    assert normalized["itens"] == [{"descricao": "X"}]


# --------------------------------------------------------------------------- #
# evaluate() — the end-to-end runner
# --------------------------------------------------------------------------- #
def test_evaluate_is_deterministic():
    first = evaluate()
    second = evaluate()
    assert first == second


def test_evaluate_result_is_json_serializable():
    result = evaluate()
    json.dumps(result)  # must not raise


def test_evaluate_reports_cases_correct_and_accuracy_per_field():
    result = evaluate()
    for field in FIELD_ORDER:
        stats = result["fields"][field]
        assert stats["cases"] == result["fixture_count"]
        assert 0 <= stats["correct"] <= stats["cases"]
        assert stats["accuracy"] == round(stats["correct"] / stats["cases"] * 100.0, 10) or (
            stats["accuracy"] == stats["correct"] / stats["cases"] * 100.0
        )


def test_evaluate_detects_an_injected_mismatch(monkeypatch):
    # REGRESSION for the evaluator itself: prove it correctly flags a real
    # difference between ground truth and extractor output. Uses an injected
    # (tampered) expectation rather than a live extractor bug, so this test
    # keeps working regardless of the extractor's current accuracy — bugs get
    # fixed (see `ambiguous_number_label`, now fixed and passing below).
    envelope, real_expected = load_fixture("clean_danfe")
    tampered_expected = dict(real_expected, numero_nf="000000000")
    monkeypatch.setattr(
        evaluate_extraction, "load_fixture", lambda name: (envelope, tampered_expected)
    )

    result = evaluate(names=["clean_danfe"])

    matches = [f for f in result["failures"] if f["field"] == "numero_nf"]
    assert len(matches) == 1
    assert matches[0]["expected"] == "000000000"
    assert matches[0]["actual"] == "001234561"
    assert result["fields"]["numero_nf"]["correct"] == 0


def test_ambiguous_number_label_fixture_now_passes():
    # The generic "numero" label used to match non-fiscal control numbers
    # ("Numero de controle: 5551234"); app/extract.py now excludes "numero
    # de/da/do <coisa>" from that fallback. This fixture must no longer appear
    # in the failures list.
    result = evaluate()
    matches = [f for f in result["failures"] if f["fixture"] == "ambiguous_number_label"]
    assert matches == []


def test_evaluate_never_leaks_full_item_rows_in_a_failure():
    # LGPD/safety: a failure on `itens` must minimize to a count, never dump
    # the row contents (which could carry fixture "PII"-shaped strings).
    result = evaluate()
    for fail in result["failures"]:
        if fail["field"] == "itens":
            for side in (fail["expected"], fail["actual"]):
                assert not isinstance(side, list)

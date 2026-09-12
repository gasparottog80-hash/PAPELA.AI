from __future__ import annotations

from app.extract import (
    EXTRACTOR_VERSION,
    extract_fields,
    parse_br_number,
    valid_cnpj,
    valid_cpf,
)


def _ocr(text: str = "", tables=None):
    """Build a minimal schema-v3 OCR envelope for the extractor."""
    page = {"page": 1, "text": text, "confidence": 1.0, "tables": tables or []}
    return {"engine": "fake", "schema_version": 3, "page_count": 1, "pages": [page]}


def _table(rows, table_id=0):
    return {"table_id": table_id, "bbox": [0, 0, 0, 0],
            "n_rows": len(rows), "n_cols": len(rows[0]) if rows else 0, "rows": rows}


# --------------------------------------------------------------------------- #
# Check-digit validators
# --------------------------------------------------------------------------- #
def test_cnpj_and_cpf_check_digits():
    # SUCCESS: real, valid documents pass.
    assert valid_cnpj("11222333000181") is True
    assert valid_cpf("52998224725") is True
    # EXPECTED FAILURE: wrong check digit and repeated-digit sequences rejected.
    assert valid_cnpj("11222333000180") is False
    assert valid_cnpj("00000000000000") is False
    assert valid_cpf("11111111111") is False


def test_parse_br_number():
    assert parse_br_number("1.234.567,89") == parse_br_number("1234567,89")
    assert str(parse_br_number("100,00")) == "100.00"
    assert parse_br_number("abc") is None


# --------------------------------------------------------------------------- #
# CNPJ extraction + role attribution
# --------------------------------------------------------------------------- #
def test_extracts_labeled_emitente_and_destinatario():
    # SUCCESS: explicit labels drive the emitente/destinatario roles.
    text = (
        "Emitente: ACME LTDA CNPJ 11.222.333/0001-81\n"
        "Destinatario: FULANO CNPJ 45.723.174/0001-10\n"
    )
    f = extract_fields(_ocr(text))
    assert f["extractor_version"] == EXTRACTOR_VERSION
    assert f["emitente_cnpj"]["value"] == "11.222.333/0001-81"
    assert f["emitente_cnpj"]["valid"] is True
    assert f["emitente_cnpj"]["role_source"] == "label"
    assert f["destinatario_cnpj"]["value"] == "45.723.174/0001-10"


def test_positional_fallback_two_unlabeled_cnpjs():
    # Two valid, unlabeled CNPJs -> (emitente, destinatario) by position, tagged.
    text = "NOTA FISCAL 11.222.333/0001-81 blah 45.723.174/0001-10 fim"
    f = extract_fields(_ocr(text))
    assert f["emitente_cnpj"]["role_source"] == "position"
    assert f["destinatario_cnpj"]["digits"] == "45723174000110"


def test_cnpj_inside_nfe_access_key_is_not_a_false_positive():
    # CRITICAL EDGE: the 44-digit NFe chave de acesso embeds digit runs that
    # look like a bare CNPJ. The digit-boundary lookarounds + check-digit gate
    # must reject them, so no spurious CNPJ is emitted from the key alone.
    chave = "3" * 44  # 44 digits, no valid CNPJ substring at a boundary
    f = extract_fields(_ocr(f"Chave de acesso: {chave}"))
    assert f["cnpjs"] == []
    assert f["emitente_cnpj"] is None


# --------------------------------------------------------------------------- #
# Scalar labeled fields
# --------------------------------------------------------------------------- #
def test_numero_nf_date_and_total():
    text = (
        "NOTA FISCAL ELETRONICA No 000123456\n"
        "Data de Emissao: 15/03/2024\n"
        "Valor Total da Nota: R$ 1.234,56\n"
    )
    f = extract_fields(_ocr(text))
    assert f["numero_nf"]["value"] == "000123456"  # leading zeros preserved
    assert f["data_emissao"]["value"] == "2024-03-15"  # ISO
    assert f["valor_total"]["value"] == "1234.56"


def test_numero_nf_ignores_access_key_and_reads_dot_grouped():
    # REGRESSION: with a 44-digit NFe access key before the real number, the
    # NF-number extractor must NOT grab a slice of the key, and must read a
    # dot-grouped number (001.234.561) as its digits. Note the key itself
    # embeds the substring "001234561" — so this also proves the labeled
    # dot-grouped token wins over a same-digits slice sitting inside the key.
    text = (
        "DANFE Nota Fiscal Eletronica\n"
        "CHAVE DE ACESSO 35240514200166000187550010001234561123456789\n"
        "NUMERO 001.234.561 SERIE 001\n"
    )
    f = extract_fields(_ocr(text))
    assert f["numero_nf"]["value"] == "001234561"


def test_numero_nf_never_extracted_from_access_key_alone():
    # REGRESSION (stronger): with ONLY the 44-digit access key and no labeled
    # NF number, the extractor must NOT synthesize a number from a slice of the
    # key. The digit-boundary + <=9-digit cap make the 44-digit run unmatchable,
    # so numero_nf is None rather than a spurious key fragment.
    text = (
        "DANFE Nota Fiscal Eletronica\n"
        "CHAVE DE ACESSO 35240514200166000187550010001234561123456789\n"
    )
    f = extract_fields(_ocr(text))
    assert f["numero_nf"] is None


def test_total_does_not_grab_unit_price_and_ignores_bare_integers():
    # CRITICAL EDGE: 'Valor Total' must bind to the total, not an unrelated
    # integer, and money regex requires ,dd cents (won't match a phone/ID).
    text = "Fone 1140028922\nValor Unitario 10,00\nValor Total 30,00\n"
    f = extract_fields(_ocr(text))
    assert f["valor_total"]["value"] == "30.00"


def test_numero_nf_ignores_generic_non_fiscal_control_number():
    # REGRESSION: a bare "numero" fallback used to match ANY occurrence of the
    # word, including non-fiscal reference numbers like "numero de controle".
    # With no recognized fiscal label present, numero_nf must stay None rather
    # than surface an unrelated control/protocol number.
    text = (
        "Comprovante de Entrega\n"
        "Numero de controle: 5551234\n"
        "Assinatura do responsavel\n"
    )
    f = extract_fields(_ocr(text))
    assert f["numero_nf"] is None


def test_missing_fields_return_none_not_error():
    f = extract_fields(_ocr("documento sem campos fiscais reconheciveis"))
    assert f["numero_nf"] is None
    assert f["valor_total"] is None
    assert f["data_emissao"] is None
    assert f["itens"] == []


# --------------------------------------------------------------------------- #
# Line items from structured tables
# --------------------------------------------------------------------------- #
def test_extracts_line_items_from_structured_table():
    rows = [
        ["Descricao", "Qtd", "Valor Unitario", "Valor Total"],
        ["Servico A", "2", "50,00", "100,00"],
        ["Produto B", "1", "250,50", "250,50"],
    ]
    f = extract_fields(_ocr("itens abaixo", tables=[_table(rows)]))
    itens = f["itens"]
    assert len(itens) == 2
    assert itens[0]["descricao"] == "Servico A"
    assert itens[0]["quantidade"] == "2"
    assert itens[0]["valor_unitario"] == "50.00"
    assert itens[0]["valor_total"] == "100.00"
    assert itens[1]["valor_total"] == "250.50"


def test_unrecognized_table_header_is_skipped():
    # A table whose header we can't map (and no description col) is not treated
    # as line items — avoids garbage items from layout tables.
    rows = [["foo", "bar"], ["1", "2"]]
    f = extract_fields(_ocr("", tables=[_table(rows)]))
    assert f["itens"] == []


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #
def test_extract_never_raises_on_malformed_input():
    # CRITICAL EDGE: a broken envelope must degrade gracefully, never raise
    # into the worker (which would fail an otherwise-good OCR job).
    for bad in ({}, {"pages": None}, {"pages": [{"text": None, "tables": None}]},
                {"pages": [{"tables": [{"rows": None}]}]}):
        f = extract_fields(bad)  # type: ignore[arg-type]
        assert f["extractor_version"] == EXTRACTOR_VERSION
        assert f["itens"] == []


def test_safe_logging_never_leaks_document_content(caplog):
    # LGPD REGRESSION: when a sub-extractor raises, _safe must log only the
    # sanitized category (no exc_info/traceback, no exception message). A prior
    # version used logger.exception, which printed a traceback embedding the
    # document content — a PII leak. Force a failure whose message carries a
    # fake CNPJ and assert none of it reaches the log record.
    import logging as _logging

    import app.extract as ex

    pii = "12.345.678/0001-99 conteudo sensivel do documento"

    def _boom(_text):
        raise ValueError(pii)

    with caplog.at_level(_logging.ERROR, logger="papela.extract"):
        got = ex._safe(_boom, ["default"], "irrelevant")

    assert got == ["default"]  # degraded to the default, did not raise
    assert len(caplog.records) == 1
    rec = caplog.records[0]
    assert rec.exc_info is None  # NO traceback attached
    # The PII must appear nowhere: not in the message, not in the sanitized
    # error field, not anywhere in the record's formatted output.
    assert pii not in rec.getMessage()
    assert pii not in str(getattr(rec, "error", ""))
    assert "12.345.678" not in caplog.text
    # What IS logged: a stable sanitized category + the failing extractor name.
    assert getattr(rec, "error", "") == "processing_error: ValueError"
    assert getattr(rec, "extractor", "") == "_boom"


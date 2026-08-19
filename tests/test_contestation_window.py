"""R7 – fereastra de contestare de la art. 28 alin. (3), și ce document produce.

Toate testele injectează `as_of`: o regulă despre termene care citește ceasul
sistemului nu mai e reproductibilă.
"""

from __future__ import annotations

from datetime import date

import pytest

from consilium.drafter import letter_mode
from consilium.reconciler import audit
from tests.conftest import apartment, expense, payment

POSTED = "2025-12-05"
DEADLINE = date(2025, 12, 15)  # 05.12 + 10 zile


def case_with_findings(posting_date: str | None = POSTED):
    """Document cu o constatare R1 reală, deci ceva de contestat."""
    document = payment(
        [
            apartment("1", {"A": 60.0}, cota=60.0),
            apartment("2", {"A": 40.0}, cota=40.0),
        ],
        [expense("A", 100.0, key="cota_indiviza")],
        total_general=1000.0,
    )
    document.posting_date = posting_date
    return document


def clean_case(posting_date: str | None = POSTED):
    document = payment(
        [
            apartment("1", {"A": 60.0}, cota=60.0),
            apartment("2", {"A": 40.0}, cota=40.0),
        ],
        [expense("A", 100.0, key="cota_indiviza")],
    )
    document.posting_date = posting_date
    return document


def r7(result):
    return result.by_rule("R7")


def coverage_for(result, rule_id="R7"):
    return next(rule for rule in result.coverage.rules if rule.rule_id == rule_id)


# --------------------------------------------------------------------------
# Fereastra deschisă
# --------------------------------------------------------------------------


def test_window_open_reports_days_remaining(config):
    result = audit(case_with_findings(), config, as_of=date(2025, 12, 10))
    findings = r7(result)
    assert len(findings) == 1
    assert findings[0].finding_type == "contestation_window_open"
    assert findings[0].severity == "info"
    assert "mai sunt 5 zile" in findings[0].message
    assert DEADLINE.isoformat() in findings[0].message


def test_last_day_of_the_window_is_still_open(config):
    findings = r7(audit(case_with_findings(), config, as_of=DEADLINE))
    assert findings[0].finding_type == "contestation_window_open"
    assert "mai sunt 0 zile" in findings[0].message


def test_window_open_cites_paragraph_three(config):
    findings = r7(audit(case_with_findings(), config, as_of=date(2025, 12, 6)))
    assert "alin. (3)" in findings[0].legal_reference


# --------------------------------------------------------------------------
# Fereastra închisă
# --------------------------------------------------------------------------


def test_window_expired_is_reported_not_silently_dropped(config):
    findings = r7(audit(case_with_findings(), config, as_of=date(2025, 12, 16)))
    assert len(findings) == 1
    assert findings[0].finding_type == "contestation_window_expired"
    assert findings[0].severity == "medium"
    assert "acum 1 zile" in findings[0].message


def test_window_expired_falls_back_to_paragraph_one(config):
    findings = r7(audit(case_with_findings(), config, as_of=date(2026, 3, 1)))
    assert "alin. (1)" in findings[0].legal_reference
    assert "listei următoare" in findings[0].message


# --------------------------------------------------------------------------
# Când R7 nu se poate pronunța
# --------------------------------------------------------------------------


def test_missing_posting_date_is_unverifiable_not_assumed(config):
    result = audit(case_with_findings(None), config, as_of=date(2025, 12, 10))
    assert r7(result) == []
    assert coverage_for(result).status == "neverificabil"
    assert "data afișării" in coverage_for(result).reason
    assert any(
        "afișat" in document for document in result.coverage.documents_to_request
    )


def test_missing_as_of_makes_r7_unverifiable(config):
    result = audit(case_with_findings(), config)
    assert r7(result) == []
    assert coverage_for(result).status == "neverificabil"
    assert "data de referință" in coverage_for(result).reason


def test_clean_document_gets_no_deadline_finding(config):
    """Fără constatări nu există ce contesta, deci nici termen de semnalat."""
    result = audit(clean_case(), config, as_of=date(2025, 12, 10))
    assert result.findings == []
    assert coverage_for(result).status == "verificat"
    assert "nicio constatare" in coverage_for(result).reason


# --------------------------------------------------------------------------
# Ce document producem
# --------------------------------------------------------------------------


def test_open_window_produces_a_contestation(config):
    result = audit(case_with_findings(), config, as_of=date(2025, 12, 10))
    assert letter_mode(result) == "contestatie"


def test_expired_window_produces_a_copies_request(config):
    result = audit(case_with_findings(), config, as_of=date(2026, 1, 20))
    assert letter_mode(result) == "cerere_copii"


def test_unknown_window_produces_a_flagged_contestation(config):
    result = audit(case_with_findings(None), config, as_of=date(2025, 12, 10))
    assert letter_mode(result) == "contestatie_termen_necunoscut"


@pytest.mark.parametrize(
    "mode",
    ["contestatie", "contestatie_termen_necunoscut", "cerere_copii"],
)
def test_every_mode_has_a_title_and_a_legal_block(config, mode):
    from consilium.drafter import legal_block

    block = legal_block(mode, config)
    assert config.require(f"drafter.mode_labels.{mode}")
    assert "196/2018" in block
    assert "{" not in block, "temei neformatat"


def test_expired_mode_forbids_invoking_paragraph_three_as_contestation(config):
    from consilium.drafter import legal_block

    block = legal_block("cerere_copii", config)
    assert "NU formula o contestație" in block
    assert "EXPIRAT" in block


def test_copies_request_never_invents_a_deadline(config):
    """Dreptul de la alin. (1) nu are termen; instrucțiunea trebuie să spună asta."""
    from consilium.drafter import legal_block

    block = legal_block("cerere_copii", config)
    assert "NU are termen" in block
    assert "nu inventa unul" in block
    assert config.require("legal.art_28_4.reference") in block


def test_open_window_block_cites_all_three_paragraphs(config):
    from consilium.drafter import legal_block

    block = legal_block("contestatie", config)
    for path in ("legal.art_28_1.reference", "legal.art_28_3.reference",
                 "legal.art_28_4.reference"):
        assert config.require(path) in block


def test_unknown_window_block_demands_the_posting_date(config):
    from consilium.drafter import legal_block

    block = legal_block("contestatie_termen_necunoscut", config)
    assert "cere confirmarea datei afișării" in block


def test_paragraph_one_has_no_deadline_in_config(config):
    assert config.require("legal.art_28_1.reference")
    assert config._data["legal"]["art_28_1"]["deadline_days"] is None

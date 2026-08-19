"""Teste pentru garanția drafter-ului: nicio sumă și nicio constatare inventată.

Verificatorul rulează complet offline. Scrisoarea este generată de model, dar
respinsă programatic dacă citează o sumă care nu provine dintr-o constatare
calculată sau dacă trimite la o constatare inexistentă.
"""

from __future__ import annotations

import pytest

from consilium.drafter import (
    _WireLetter,
    _WireParagraph,
    allowed_amounts,
    parse_money,
    verify_letter,
)
from consilium.reconciler import audit
from tests.conftest import apartment, expense, payment


@pytest.fixture
def case(config):
    """Un document cu trei constatări: R1, R2 și R4."""
    document = payment(
        [
            apartment("1", {"A": 60.0}, cota=60.0, arrears=1000.0, penalties=150.0),
            apartment("2", {"A": 40.0}, cota=35.0),
        ],
        [expense("A", 100.0, key="cota_indiviza")],
        total_general=1000.0,
    )
    return document, audit(document, config)


def letter(paragraphs: list[_WireParagraph], **overrides) -> _WireLetter:
    base = {
        "subject": "Cerere de documente",
        "opening": "Vă adresez prezenta cerere.",
        "finding_paragraphs": paragraphs,
        "coverage_paragraph": "Toate pozițiile au fost verificate.",
        "requested_documents": ["Facturile furnizorilor."],
        "deadline_paragraph": "Termen de 10 zile.",
        "closing": "Cu stimă,",
    }
    base.update(overrides)
    return _WireLetter(**base)


def faithful(result) -> list[_WireParagraph]:
    return [
        _WireParagraph(
            finding_index=index,
            rule_id=finding.rule_id,
            text=f"Constatare privind {finding.rule_id}.",
        )
        for index, finding in enumerate(result.findings)
    ]


# --------------------------------------------------------------------------
# Cazul bun
# --------------------------------------------------------------------------


def test_faithful_letter_passes(case):
    _, result = case
    document, _ = case
    assert verify_letter(letter(faithful(result)), result, document).ok


def test_letter_may_quote_amounts_that_come_from_findings(case):
    document, result = case
    paragraphs = faithful(result)
    paragraphs[0] = _WireParagraph(
        finding_index=0,
        rule_id=result.findings[0].rule_id,
        text="Diferența constatată este de 900,00 lei.",
    )
    assert verify_letter(letter(paragraphs), result, document).ok


# --------------------------------------------------------------------------
# Sume inventate
# --------------------------------------------------------------------------


def test_invented_amount_is_rejected(case):
    document, result = case
    paragraphs = faithful(result)
    paragraphs[0] = _WireParagraph(
        finding_index=0,
        rule_id=result.findings[0].rule_id,
        text="Prejudiciul total se ridică la 12.345,67 lei.",
    )
    verification = verify_letter(letter(paragraphs), result, document)
    assert not verification.ok
    assert any("12.345,67" in violation for violation in verification.violations)


def test_recomputed_amount_is_rejected(case):
    """O sumă corect calculată, dar necalculată de reconciler, tot e respinsă."""
    document, result = case
    from consilium.drafter import format_money

    total = round(
        sum(f.amount_involved or 0.0 for f in result.findings) + 0.01, 2
    )
    assert total not in allowed_amounts(result, document)
    paragraphs = faithful(result)
    paragraphs[0] = _WireParagraph(
        finding_index=0,
        rule_id=result.findings[0].rule_id,
        text=f"Însumând constatările rezultă {format_money(total)} lei.",
    )
    verification = verify_letter(letter(paragraphs), result, document)
    assert not verification.ok
    assert any("nu provine" in violation for violation in verification.violations)


def test_invented_amount_in_the_closing_is_also_caught(case):
    document, result = case
    verification = verify_letter(
        letter(faithful(result), closing="Vă solicit restituirea a 42,00 lei."),
        result,
        document,
    )
    assert not verification.ok


# --------------------------------------------------------------------------
# Constatări inventate, greșit atribuite sau omise
# --------------------------------------------------------------------------


def test_paragraph_pointing_to_a_nonexistent_finding_is_rejected(case):
    document, result = case
    paragraphs = faithful(result)
    paragraphs.append(
        _WireParagraph(finding_index=99, rule_id="R1", text="Alt paragraf.")
    )
    verification = verify_letter(letter(paragraphs), result, document)
    assert not verification.ok
    assert any("inexistentă" in violation for violation in verification.violations)


def test_wrong_rule_id_on_a_real_finding_is_rejected(case):
    document, result = case
    paragraphs = faithful(result)
    paragraphs[0] = _WireParagraph(
        finding_index=0, rule_id="R6", text="Constatare atribuită greșit."
    )
    verification = verify_letter(letter(paragraphs), result, document)
    assert not verification.ok
    assert any("R6" in violation for violation in verification.violations)


def test_omitting_a_finding_is_rejected(case):
    document, result = case
    verification = verify_letter(letter(faithful(result)[:-1]), result, document)
    assert not verification.ok
    assert any("omise" in violation for violation in verification.violations)


def test_citing_a_finding_twice_is_rejected(case):
    document, result = case
    paragraphs = faithful(result)
    paragraphs.append(paragraphs[0])
    verification = verify_letter(letter(paragraphs), result, document)
    assert not verification.ok
    assert any("mai multe ori" in violation for violation in verification.violations)


# --------------------------------------------------------------------------
# Mecanica
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "value"),
    [("1.234,56", 1234.56), ("96,00", 96.0), ("26.789,95", 26789.95)],
)
def test_romanian_money_parsing(token, value):
    assert parse_money(token) == pytest.approx(value)


def test_allowlist_contains_every_finding_value(case):
    document, result = case
    allowed = allowed_amounts(result, document)
    for finding in result.findings:
        for value in (
            finding.expected_value,
            finding.found_value,
            finding.amount_involved,
        ):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                assert round(float(value), 2) in allowed


def test_every_letter_claim_maps_to_a_real_rule_id(case):
    """Constrângerea centrală: fiecare afirmație are un rule_id existent."""
    document, result = case
    draft = letter(faithful(result))
    assert verify_letter(draft, result, document).ok
    real_rules = {finding.rule_id for finding in result.findings}
    assert {p.rule_id for p in draft.finding_paragraphs} <= real_rules

"""Testul de acceptanță: reconciler-ul contra constatărilor plantate.

Rulează pe extracțiile reale salvate în samples/extracted/, comparate cu
samples/synthetic/expected_findings.json, documentul care spune exact ce erori
a plantat generatorul. Nu se face niciun apel de model: extracțiile sunt deja pe
disc, iar reconciler-ul e determinist.

Criteriul: toate constatările plantate sunt găsite pe sample_errors și
sample_penalties, și ZERO pe sample_clean. Un fals pozitiv pe documentul curat
este eșec, nu zgomot acceptabil.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from consilium.config import Config
from consilium.integrity import check_integrity
from consilium.reconciler import audit
from consilium.schema import PaymentList

SYNTHETIC = Path("samples/synthetic")
EXTRACTED = Path("samples/extracted")

pytestmark = pytest.mark.skipif(
    not (EXTRACTED / "sample_clean.json").is_file(),
    reason="lipsesc extracțiile din samples/extracted/ (rulează consilium.extractor)",
)


@pytest.fixture(scope="module")
def expected() -> dict:
    return json.loads(
        (SYNTHETIC / "expected_findings.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def real_config() -> Config:
    return Config.load("config.yaml")


def load(name: str) -> PaymentList:
    return PaymentList.model_validate_json(
        (EXTRACTED / f"{name}.json").read_text(encoding="utf-8")
    )


def planted_for(expected: dict, sample: str) -> list[dict]:
    """Constatările plantate pentru un sample, fără cele de acoperire."""
    return [
        finding
        for finding in expected["findings"]
        if finding["sample"] == sample and finding["type"] != "unverifiable_distribution"
    ]


# --------------------------------------------------------------------------
# Criteriul principal
# --------------------------------------------------------------------------


def test_clean_document_produces_no_findings(real_config):
    result = audit(load("sample_clean"), real_config)
    assert result.findings == [], (
        "fals pozitiv pe documentul curat: "
        + "; ".join(f.message for f in result.findings)
    )


def test_clean_document_is_fully_covered(real_config):
    coverage = audit(load("sample_clean"), real_config).coverage
    assert coverage.expense_lines_unverified == []
    assert len(coverage.expense_lines_verified) == 8
    assert all(rule.status == "verificat" for rule in coverage.rules)


def test_errors_document_finds_every_planted_finding(expected, real_config):
    planted = planted_for(expected, "sample_errors.pdf")
    found = audit(load("sample_errors"), real_config).findings
    assert Counter(f["rule_id"] for f in planted) == Counter(
        f.rule_id for f in found
    )


def test_penalties_document_finds_every_planted_finding(expected, real_config):
    planted = planted_for(expected, "sample_penalties.pdf")
    found = audit(load("sample_penalties"), real_config).findings
    assert Counter(f["rule_id"] for f in planted) == Counter(
        f.rule_id for f in found
    )


# --------------------------------------------------------------------------
# Fiecare constatare plantată, individual
# --------------------------------------------------------------------------


def test_err001_declared_total_mismatch(expected, real_config):
    planted = next(f for f in expected["findings"] if f["id"] == "ERR-001")
    finding = audit(load("sample_errors"), real_config).by_rule("R1")[0]
    assert finding.found_value == pytest.approx(planted["found_value"])
    assert finding.expected_value == pytest.approx(planted["expected_value"])
    assert finding.amount_involved == pytest.approx(planted["amount_involved"])


def test_err002_wrong_distribution_key(expected, real_config):
    planted = next(f for f in expected["findings"] if f["id"] == "ERR-002")
    findings = audit(load("sample_errors"), real_config).by_rule("R3")
    assert len(findings) == 1
    assert findings[0].category == planted["location"]["column"]
    assert findings[0].amount_involved == pytest.approx(
        planted["amount_involved"], abs=0.05
    )


def test_err003_apartment_sum_mismatch(expected, real_config):
    planted = next(f for f in expected["findings"] if f["id"] == "ERR-003")
    finding = audit(load("sample_errors"), real_config).by_rule("R5")[0]
    assert finding.amount_involved == pytest.approx(abs(planted["amount_involved"]))


def test_pen001_cota_sum_mismatch(expected, real_config):
    planted = next(f for f in expected["findings"] if f["id"] == "PEN-001")
    finding = audit(load("sample_penalties"), real_config).by_rule("R2")[0]
    assert finding.found_value == pytest.approx(planted["found_value"])
    assert finding.expected_value == pytest.approx(planted["expected_value"])


@pytest.mark.parametrize("finding_id", ["PEN-002", "PEN-003", "PEN-004"])
def test_penalty_over_cap(expected, real_config, finding_id):
    planted = next(f for f in expected["findings"] if f["id"] == finding_id)
    apartment_no = planted["location"]["apartment_no"]
    findings = audit(load("sample_penalties"), real_config).by_rule("R4")
    finding = next(f for f in findings if f.apartment_no == apartment_no)
    assert finding.found_value == pytest.approx(planted["found_value"])
    assert finding.expected_value == pytest.approx(planted["expected_value"])
    assert finding.amount_involved == pytest.approx(planted["amount_involved"])
    assert finding.legal_reference == planted["legal_reference"]


# --------------------------------------------------------------------------
# Acoperire: ce nu s-a putut verifica trebuie raportat, nu ascuns
# --------------------------------------------------------------------------


def test_cov001_missing_consumption_annex_is_reported(expected, real_config):
    planted = next(f for f in expected["findings"] if f["id"] == "COV-001")
    coverage = audit(load("sample_errors"), real_config).coverage
    unverified = [category for category, _ in coverage.expense_lines_unverified]
    assert unverified == planted["affected_categories"]
    assert len(coverage.expense_lines_verified) == 6
    assert any("consum" in doc for doc in coverage.documents_to_request)


def test_coverage_matches_generator_expectation(expected, real_config):
    for sample, meta in expected["samples"].items():
        if "_scanned" in sample or "expected_coverage" not in meta:
            continue
        # Generatorul poate produce variante pentru care nu exista inca o
        # extractie pe disc; acoperirea se verifica pe cele disponibile.
        if not (EXTRACTED / f"{Path(sample).stem}.json").is_file():
            continue
        coverage = audit(load(Path(sample).stem), real_config).coverage
        assert coverage.expense_lines_total == meta["expected_coverage"][
            "expense_lines_total"
        ]
        assert len(coverage.expense_lines_verified) == meta["expected_coverage"][
            "expense_lines_verifiable"
        ], sample


def test_supplier_invoices_are_always_requested(real_config):
    """Coerența internă nu înseamnă că sumele sunt reale."""
    coverage = audit(load("sample_clean"), real_config).coverage
    assert any("acturi" in doc for doc in coverage.documents_to_request)


# --------------------------------------------------------------------------
# R0 pe documentele native: nicio suspiciune de transcriere
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["sample_clean", "sample_errors", "sample_penalties"]
)
def test_r0_is_clean_on_native_documents(real_config, name):
    report = check_integrity(load(name), real_config)
    assert report.is_clean, "; ".join(issue.message for issue in report.issues)
    assert report.rows_checked == 28
    assert report.columns_checked == 8


# --------------------------------------------------------------------------
# Documentul scanat: R0 prinde eroarea de citire fără să inventeze un audit
# --------------------------------------------------------------------------

SCANNED = EXTRACTED / "sample_clean_scanned.json"


def scanned_resolution(real_config):
    """Reproduce rezoluția obținută la recitirea țintită, fără apel de model."""
    from consilium.integrity import Resolution

    payment = load("sample_clean_scanned")
    report = check_integrity(payment, real_config)
    return payment, Resolution(
        report=report,
        confirmed_inconsistencies=[],
        unauditable_apartments={"7"},
        unauditable_categories={"Apă caldă menajeră"},
        low_confidence_fields=[
            "apartment_lines[7].charges['Apă caldă menajeră']"
        ],
        reread_calls=1,
    )


@pytest.mark.skipif(not SCANNED.is_file(), reason="lipsește extracția scanată")
def test_r0_catches_the_misread_cell_on_the_scan(real_config):
    report = check_integrity(load("sample_clean_scanned"), real_config)
    assert not report.is_clean
    assert [issue.apartment_no for issue in report.row_issues] == ["7"]
    assert [issue.category for issue in report.column_issues] == [
        "Apă caldă menajeră"
    ]
    assert [(c.apartment_no, c.category) for c in report.suspect_cells] == [
        ("7", "Apă caldă menajeră")
    ]


@pytest.mark.skipif(not SCANNED.is_file(), reason="lipsește extracția scanată")
def test_scan_produces_no_false_audit_findings(real_config):
    """O celulă citită greșit nu trebuie să devină o acuzație către asociație."""
    payment, resolution = scanned_resolution(real_config)
    result = audit(payment, real_config, resolution)
    assert result.findings == [], (
        "fals pozitiv provocat de scanare: "
        + "; ".join(f.message for f in result.findings)
    )


@pytest.mark.skipif(not SCANNED.is_file(), reason="lipsește extracția scanată")
def test_scan_reports_what_it_could_not_verify(real_config):
    payment, resolution = scanned_resolution(real_config)
    coverage = audit(payment, real_config, resolution).coverage
    unverified = [category for category, _ in coverage.expense_lines_unverified]
    assert "Apă caldă menajeră" in unverified
    assert any("7" in reason for _, reason in coverage.expense_lines_unverified)
    assert any("original" in doc for doc in coverage.documents_to_request)


@pytest.mark.skipif(not SCANNED.is_file(), reason="lipsește extracția scanată")
def test_scan_still_audits_the_rules_the_bad_cell_does_not_touch(real_config):
    """Un câmp incert nu trebuie să anuleze tot auditul."""
    payment, resolution = scanned_resolution(real_config)
    coverage = audit(payment, real_config, resolution).coverage
    statuses = {rule.rule_id: rule.status for rule in coverage.rules}
    assert statuses["R2"] == "verificat"
    assert statuses["R4"] == "verificat"
    assert statuses["R5"] == "verificat"
    assert statuses["R6"] == "verificat"
    assert statuses["R3"] == "parțial"

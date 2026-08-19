"""Teste pentru reconciler. Minimum două per regulă, toate offline.

Dacă vreunul dintre testele de aici ar avea nevoie de un model ca să treacă,
testul e greșit: reconciler-ul este determinist prin construcție.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from consilium.reconciler import audit, rule_r1, rule_r6
from consilium.schema import Consumption
from tests.conftest import apartment, expense, payment


def rule_ids(result) -> list[str]:
    return sorted(finding.rule_id for finding in result.findings)


# --------------------------------------------------------------------------
# R1 – suma pozițiilor față de totalul general declarat
# --------------------------------------------------------------------------


def test_r1_passes_when_lines_sum_to_declared_total(config):
    document = payment(
        [apartment("1", {"A": 60.0}), apartment("2", {"A": 40.0})],
        [expense("A", 100.0)],
    )
    assert rule_r1(document, config) == []


def test_r1_catches_inflated_declared_total(config):
    document = payment(
        [apartment("1", {"A": 100.0})], [expense("A", 100.0)], total_general=1000.0
    )
    findings = rule_r1(document, config)
    assert len(findings) == 1
    assert findings[0].amount_involved == pytest.approx(900.0)
    assert findings[0].found_value == pytest.approx(1000.0)
    assert findings[0].expected_value == pytest.approx(100.0)


def test_r1_respects_configured_tolerance(config):
    document = payment(
        [apartment("1", {"A": 100.0})], [expense("A", 100.0)], total_general=100.005
    )
    assert rule_r1(document, config) == []


# --------------------------------------------------------------------------
# R2 – suma cotelor indivize
# --------------------------------------------------------------------------


def test_r2_passes_when_cotas_sum_to_one_hundred(config):
    document = payment(
        [
            apartment("1", {"A": 50.0}, cota=60.0),
            apartment("2", {"A": 50.0}, cota=40.0),
        ],
        [expense("A", 100.0, key="cota_indiviza")],
    )
    assert audit(document, config).by_rule("R2") == []


def test_r2_catches_cotas_below_one_hundred(config):
    document = payment(
        [
            apartment("1", {"A": 50.0}, cota=60.0),
            apartment("2", {"A": 50.0}, cota=39.2),
        ],
        [expense("A", 100.0, key="cota_indiviza")],
    )
    findings = audit(document, config).by_rule("R2")
    assert len(findings) == 1
    assert findings[0].found_value == pytest.approx(99.2)
    assert "lipsesc" in findings[0].message


def test_r2_catches_cotas_above_one_hundred(config):
    document = payment(
        [
            apartment("1", {"A": 50.0}, cota=60.0),
            apartment("2", {"A": 50.0}, cota=45.0),
        ],
        [expense("A", 100.0, key="cota_indiviza")],
    )
    findings = audit(document, config).by_rule("R2")
    assert findings[0].found_value == pytest.approx(105.0)
    assert "sunt în plus" in findings[0].message


# --------------------------------------------------------------------------
# R3 – repartizarea conform cheii declarate
# --------------------------------------------------------------------------


def test_r3_accepts_correct_distribution_by_persons(config):
    document = payment(
        [
            apartment("1", {"Salubritate": 25.0}, persons=1, cota=50.0),
            apartment("2", {"Salubritate": 75.0}, persons=3, cota=50.0),
        ],
        [expense("Salubritate", 100.0, key="persoane")],
    )
    assert audit(document, config).by_rule("R3") == []


def test_r3_accepts_rounding_remainder_on_last_apartment(config):
    # 100 lei în părți egale la 3 apartamente: 33,33 + 33,33 + 33,34.
    document = payment(
        [
            apartment("1", {"Administrare": 33.33}, cota=33.34),
            apartment("2", {"Administrare": 33.33}, cota=33.33),
            apartment("3", {"Administrare": 33.34}, cota=33.33),
        ],
        [expense("Administrare", 100.0, key="egal")],
    )
    assert audit(document, config).by_rule("R3") == []


def test_r3_catches_expense_distributed_on_wrong_key(config):
    # Declarată pe cotă indiviză, repartizată de fapt pe număr de persoane.
    document = payment(
        [
            apartment("1", {"Căldură": 25.0}, persons=1, cota=50.0),
            apartment("2", {"Căldură": 75.0}, persons=3, cota=50.0),
        ],
        [expense("Căldură", 100.0, key="cota_indiviza")],
    )
    findings = audit(document, config).by_rule("R3")
    assert len(findings) == 1
    assert findings[0].finding_type == "wrong_distribution_key"
    assert findings[0].amount_involved == pytest.approx(25.0)


def test_r3_catches_skimming_under_the_per_apartment_threshold(config):
    """Câțiva bani luați de la fiecare apartament, sub orice prag individual.

    Este exact cazul pentru care pragul agregat există: nicio deviație
    individuală nu depășește 0,05 lei, dar toate merg în aceeași direcție.
    """
    share = round(100.0 / 28, 2)
    apartments = [
        apartment(str(index), {"Lift": share + 0.03}, cota=round(100 / 28, 2))
        for index in range(1, 29)
    ]
    document = payment(apartments, [expense("Lift", 100.0, key="egal")])
    findings = audit(document, config).by_rule("R3")
    drift = [f for f in findings if f.finding_type == "systematic_distribution_drift"]
    assert len(drift) == 1
    # 28 x 0,0286 lei = 0,80 lei mutați, fără ca vreo celulă să pară anormală.
    assert drift[0].found_value == pytest.approx(0.80, abs=0.01)
    assert not [f for f in findings if f.finding_type == "wrong_distribution_key"]


def test_r3_reports_consumption_lines_as_unverifiable_when_annex_missing(config):
    document = payment(
        [apartment("1", {"Apă rece": 60.0}), apartment("2", {"Apă rece": 40.0})],
        [expense("Apă rece", 100.0, key="consum")],
    )
    result = audit(document, config)
    assert result.by_rule("R3") == []
    assert result.coverage.expense_lines_verified == []
    assert len(result.coverage.expense_lines_unverified) == 1
    category, reason = result.coverage.expense_lines_unverified[0]
    assert category == "Apă rece"
    assert "consumurile individuale" in reason


def test_r3_verifies_consumption_lines_when_annex_present(config):
    document = payment(
        [
            apartment(
                "1", {"Apă rece": 60.0}, consumption=Consumption(apa_rece_mc=6.0)
            ),
            apartment(
                "2", {"Apă rece": 40.0}, consumption=Consumption(apa_rece_mc=4.0)
            ),
        ],
        [expense("Apă rece", 100.0, key="consum")],
    )
    result = audit(document, config)
    assert result.by_rule("R3") == []
    assert result.coverage.expense_lines_verified == ["Apă rece"]


def test_r3_catches_wrong_consumption_split_when_annex_present(config):
    document = payment(
        [
            apartment(
                "1", {"Apă rece": 20.0}, consumption=Consumption(apa_rece_mc=6.0)
            ),
            apartment(
                "2", {"Apă rece": 80.0}, consumption=Consumption(apa_rece_mc=4.0)
            ),
        ],
        [expense("Apă rece", 100.0, key="consum")],
    )
    findings = audit(document, config).by_rule("R3")
    assert [f.finding_type for f in findings] == ["wrong_distribution_key"]


# --------------------------------------------------------------------------
# R4 – plafonul legal de penalizare
# --------------------------------------------------------------------------


def test_r4_accepts_penalty_at_the_legal_cap(config):
    # 1000 lei restanță, 30 de zile, 0,2%/zi -> exact 60 lei.
    document = payment(
        [apartment("1", {"A": 10.0}, arrears=1000.0, penalties=60.0)],
        [expense("A", 10.0)],
    )
    assert audit(document, config).by_rule("R4") == []


def test_r4_accepts_zero_penalty_without_arrears(config):
    document = payment([apartment("1", {"A": 10.0})], [expense("A", 10.0)])
    assert audit(document, config).by_rule("R4") == []


def test_r4_catches_penalty_above_the_legal_cap(config):
    document = payment(
        [apartment("1", {"A": 10.0}, arrears=1000.0, penalties=150.0)],
        [expense("A", 10.0)],
    )
    findings = audit(document, config).by_rule("R4")
    assert len(findings) == 1
    assert findings[0].expected_value == pytest.approx(60.0)
    assert findings[0].amount_involved == pytest.approx(90.0)
    assert "0.50%/zi" in findings[0].message


def test_r4_catches_penalty_charged_without_any_arrears(config):
    document = payment(
        [apartment("1", {"A": 10.0}, arrears=0.0, penalties=12.0)],
        [expense("A", 10.0)],
    )
    findings = audit(document, config).by_rule("R4")
    assert len(findings) == 1
    assert "fără restanță" in findings[0].message


# --------------------------------------------------------------------------
# R5 – cheltuiala lunii curente pe apartamente
# --------------------------------------------------------------------------


def test_r5_passes_when_current_charges_match_declared_total(config):
    document = payment(
        [apartment("1", {"A": 60.0}), apartment("2", {"A": 40.0})],
        [expense("A", 100.0)],
    )
    assert audit(document, config).by_rule("R5") == []


def test_r5_ignores_arrears_and_penalties(config):
    """Restanțele sunt istoric: nu trebuie să producă o constatare pe o listă corectă."""
    document = payment(
        [
            apartment("1", {"A": 60.0}, arrears=500.0, penalties=30.0),
            apartment("2", {"A": 40.0}),
        ],
        [expense("A", 100.0)],
    )
    assert audit(document, config).by_rule("R5") == []


def test_r5_catches_current_charges_below_declared_total(config):
    document = payment(
        [apartment("1", {"A": 60.0}), apartment("2", {"A": 40.0})],
        [expense("A", 100.0)],
        total_general=1000.0,
    )
    findings = audit(document, config).by_rule("R5")
    assert len(findings) == 1
    assert findings[0].amount_involved == pytest.approx(900.0)


# --------------------------------------------------------------------------
# R6 – numărul de apartamente
# --------------------------------------------------------------------------


def test_r6_passes_when_count_matches(config):
    document = payment([apartment("1", {"A": 100.0})], [expense("A", 100.0)])
    assert rule_r6(document)[0] == []


def test_r6_catches_missing_apartments(config):
    document = payment(
        [apartment("1", {"A": 100.0})], [expense("A", 100.0)], apartment_count=28
    )
    findings, _ = rule_r6(document)
    assert len(findings) == 1
    assert findings[0].expected_value == 28
    assert findings[0].found_value == 1


def test_r6_catches_extra_apartments(config):
    document = payment(
        [apartment("1", {"A": 50.0}), apartment("2", {"A": 50.0})],
        [expense("A", 100.0)],
        apartment_count=1,
    )
    findings, _ = rule_r6(document)
    assert findings[0].found_value == 2


# --------------------------------------------------------------------------
# Proprietăți ale modulului
# --------------------------------------------------------------------------


def test_reconciler_source_imports_no_model_sdk():
    """Interdicție structurală, verificată pe AST, nu pe convenție."""
    tree = ast.parse(Path("consilium/reconciler.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"google", "genai", "httpx", "requests", "urllib", "socket", "aiohttp"}
    assert not (imported & forbidden), f"import interzis: {imported & forbidden}"


def test_audit_is_deterministic(config):
    document = payment(
        [
            apartment("1", {"A": 60.0}, arrears=1000.0, penalties=150.0, cota=60.0),
            apartment("2", {"A": 40.0}, cota=39.0),
        ],
        [expense("A", 100.0, key="cota_indiviza")],
        total_general=200.0,
    )
    first = audit(document, config)
    second = audit(document, config)
    assert [f.message for f in first.findings] == [f.message for f in second.findings]
    assert rule_ids(first) == rule_ids(second)


def test_missing_config_key_crashes_instead_of_defaulting(config):
    from consilium.config import Config, ConfigError

    broken = Config({"reconciler": {"total_tolerance": 0.01}})
    document = payment([apartment("1", {"A": 100.0})], [expense("A", 100.0)])
    with pytest.raises(ConfigError, match="cota_sum_target"):
        audit(document, broken)

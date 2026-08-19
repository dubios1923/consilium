"""Teste pentru R0 – coerența transcrierii. Complet offline."""

from __future__ import annotations

import pytest

from consilium.config import Config, ConfigError
from consilium.integrity import (
    RULE_COLUMN,
    RULE_ROW,
    check_columns,
    check_integrity,
    check_rows,
    localize,
    without_reread,
)
from consilium.schema import (
    ApartmentLine,
    DeclaredTotals,
    ExpenseLine,
    PaymentList,
)

CONFIG = Config(
    {
        "integrity": {
            "row_total_tolerance": 0.01,
            "column_total_tolerance": 0.01,
            "reread_dpi": 300,
            "reread_match_tolerance": 0.01,
        }
    }
)


def make_payment(
    apartments: list[ApartmentLine], expenses: list[ExpenseLine]
) -> PaymentList:
    return PaymentList(
        document_id="LP-TEST",
        association_ref="Asociație de test",
        period="2025-11",
        declared_totals=DeclaredTotals(
            total_general=sum(line.amount for line in expenses),
            apartment_count=len(apartments),
        ),
        expense_lines=expenses,
        apartment_lines=apartments,
    )


def apartment(
    number: str,
    charges: dict[str, float],
    arrears: float = 0.0,
    penalties: float = 0.0,
    total_due: float | None = None,
) -> ApartmentLine:
    computed = round(sum(charges.values()) + arrears + penalties, 2)
    return ApartmentLine(
        apartment_no=number,
        persons=2,
        cota_indiviza=50.0,
        charges=charges,
        arrears=arrears,
        penalties=penalties,
        total_due=computed if total_due is None else total_due,
    )


# --------------------------------------------------------------------------
# R0.row
# --------------------------------------------------------------------------


def test_row_check_passes_when_breakdown_matches_total():
    lines = [apartment("1", {"Apă": 10.0, "Lift": 5.0}, arrears=2.0, penalties=0.5)]
    assert check_rows(make_payment(lines, []), 0.01) == []


def test_row_check_catches_missing_amount():
    lines = [apartment("1", {"Apă": 10.0, "Lift": 5.0}, total_due=20.0)]
    issues = check_rows(make_payment(lines, []), 0.01)
    assert len(issues) == 1
    assert issues[0].rule_id == RULE_ROW
    assert issues[0].apartment_no == "1"
    assert issues[0].delta == pytest.approx(-5.0)


def test_row_check_counts_arrears_and_penalties():
    lines = [apartment("1", {"Apă": 10.0}, arrears=100.0, penalties=6.0, total_due=10.0)]
    issues = check_rows(make_payment(lines, []), 0.01)
    assert issues[0].delta == pytest.approx(106.0)


def test_row_check_respects_tolerance():
    lines = [apartment("1", {"Apă": 10.0}, total_due=10.005)]
    assert check_rows(make_payment(lines, []), 0.01) == []
    assert len(check_rows(make_payment(lines, []), 0.001)) == 1


# --------------------------------------------------------------------------
# R0.column
# --------------------------------------------------------------------------


def test_column_check_passes_when_column_sums_to_declared():
    expenses = [ExpenseLine(category="Apă", amount=30.0, distribution_key="consum")]
    lines = [apartment("1", {"Apă": 10.0}), apartment("2", {"Apă": 20.0})]
    assert check_columns(make_payment(lines, expenses), 0.01) == []


def test_column_check_catches_short_column():
    expenses = [ExpenseLine(category="Apă", amount=30.0, distribution_key="consum")]
    lines = [apartment("1", {"Apă": 10.0}), apartment("2", {"Apă": 12.4})]
    issues = check_columns(make_payment(lines, expenses), 0.01)
    assert len(issues) == 1
    assert issues[0].rule_id == RULE_COLUMN
    assert issues[0].category == "Apă"
    assert issues[0].delta == pytest.approx(-7.6)


def test_column_check_flags_apartments_missing_the_column():
    expenses = [ExpenseLine(category="Apă", amount=10.0, distribution_key="consum")]
    lines = [apartment("1", {"Apă": 10.0}), apartment("2", {"Lift": 3.0})]
    issues = check_columns(make_payment(lines, expenses), 0.01)
    assert len(issues) == 1
    assert "nu au deloc această coloană" in issues[0].message


# --------------------------------------------------------------------------
# Localizarea celulei
# --------------------------------------------------------------------------


def test_localize_intersects_row_and_column_with_equal_delta():
    expenses = [
        ExpenseLine(category="Apă", amount=30.0, distribution_key="consum"),
        ExpenseLine(category="Lift", amount=10.0, distribution_key="persoane"),
    ]
    lines = [
        apartment("1", {"Apă": 10.0, "Lift": 5.0}),
        apartment("2", {"Apă": 12.4, "Lift": 5.0}, total_due=25.0),
    ]
    report = check_integrity(make_payment(lines, expenses), CONFIG)
    assert [(c.apartment_no, c.category) for c in report.suspect_cells] == [
        ("2", "Apă")
    ]


def test_localize_stays_silent_when_two_columns_match():
    payment = make_payment(
        [apartment("1", {"A": 1.0, "B": 1.0}, total_due=3.0)],
        [
            ExpenseLine(category="A", amount=2.0, distribution_key="egal"),
            ExpenseLine(category="B", amount=2.0, distribution_key="egal"),
        ],
    )
    row_issues = check_rows(payment, 0.01)
    column_issues = check_columns(payment, 0.01)
    assert len(row_issues) == 1
    assert len(column_issues) == 2
    # Ambele coloane lipsesc exact cât lipsește rândul: intersecția e ambiguă.
    assert localize(row_issues, column_issues, 0.01) == []


# --------------------------------------------------------------------------
# Rezoluția fără recitire și configul
# --------------------------------------------------------------------------


def test_without_reread_marks_everything_unauditable():
    expenses = [ExpenseLine(category="Apă", amount=30.0, distribution_key="consum")]
    lines = [apartment("1", {"Apă": 10.0}, total_due=99.0)]
    report = check_integrity(make_payment(lines, expenses), CONFIG)
    resolution = without_reread(report)
    assert resolution.unauditable_apartments == {"1"}
    assert resolution.confirmed_inconsistencies == []
    assert not resolution.is_fully_auditable


def test_missing_config_key_crashes_instead_of_defaulting():
    config = Config({"integrity": {"row_total_tolerance": 0.01}})
    payment = make_payment([apartment("1", {"Apă": 1.0})], [])
    with pytest.raises(ConfigError, match="column_total_tolerance"):
        check_integrity(payment, config)


def test_clean_document_reports_no_issues():
    expenses = [ExpenseLine(category="Apă", amount=30.0, distribution_key="consum")]
    lines = [apartment("1", {"Apă": 10.0}), apartment("2", {"Apă": 20.0})]
    report = check_integrity(make_payment(lines, expenses), CONFIG)
    assert report.is_clean
    assert report.rows_checked == 2
    assert report.columns_checked == 1

"""Utilitare comune pentru testele Consilium. Totul offline."""

from __future__ import annotations

import pytest

from consilium.config import Config
from consilium.schema import (
    ApartmentLine,
    Consumption,
    DeclaredTotals,
    ExpenseLine,
    PaymentList,
)

CONFIG_DATA = {
    "integrity": {
        "row_total_tolerance": 0.01,
        "column_total_tolerance": 0.01,
        "reread_dpi": 300,
        "reread_match_tolerance": 0.01,
    },
    "reconciler": {
        "total_tolerance": 0.01,
        "cota_sum_target": 100.0,
        "cota_sum_tolerance": 0.01,
        "distribution_per_apartment_tolerance": 0.05,
        "distribution_aggregate_tolerance": 0.01,
        "consumption_field_keywords": {
            "apa_rece_mc": ["aparece"],
            "apa_calda_mc": ["apacalda"],
            "caldura_gcal": ["energietermica", "caldura"],
        },
        "penalty_max_rate_per_day": 0.002,
        "penalty_days": 30,
        "apartment_total_tolerance": 0.01,
        "legal_references": {
            "R2": "Legea 196/2018 – cote indivize",
            "R3": "Legea 196/2018 – repartizare",
            "R4": "Legea 196/2018, art. 77 alin. (2)",
        },
    },
    "legal": {
        "art_28_1": {
            "reference": "art. 28 alin. (1) din Legea nr. 196/2018",
            "right": "dreptul de a primi copii după documentele asociației",
            "deadline_days": None,
        },
        "art_28_3": {
            "reference": "art. 28 alin. (3) din Legea nr. 196/2018",
            "right": "dreptul de a contesta modul de calcul al cotei",
            "contestation_window_days": 10,
            "response_days": 10,
        },
        "art_28_4": {
            "reference": "art. 28 alin. (4) din Legea nr. 196/2018",
            "right": "sesizarea autorității administrației publice locale",
            "unresolved_days": 10,
        },
    },
    "drafter": {
        "mode_labels": {
            "contestatie": "CONTESTAȚIE PRIVIND MODUL DE CALCUL AL COTEI",
            "contestatie_termen_necunoscut": (
                "CONTESTAȚIE PRIVIND MODUL DE CALCUL AL COTEI"
            ),
            "cerere_copii": "CERERE DE COMUNICARE A DOCUMENTELOR",
        }
    },
}


@pytest.fixture
def config() -> Config:
    return Config(CONFIG_DATA)


def expense(
    category: str, amount: float, key: str = "egal", ref: str | None = "DOC-1"
) -> ExpenseLine:
    return ExpenseLine(
        category=category,
        amount=amount,
        distribution_key=key,  # type: ignore[arg-type]
        source_invoice_ref=ref,
    )


def apartment(
    number: str,
    charges: dict[str, float],
    *,
    persons: int = 2,
    cota: float = 25.0,
    arrears: float = 0.0,
    penalties: float = 0.0,
    total_due: float | None = None,
    consumption: Consumption | None = None,
) -> ApartmentLine:
    computed = round(sum(charges.values()) + arrears + penalties, 2)
    return ApartmentLine(
        apartment_no=number,
        persons=persons,
        cota_indiviza=cota,
        charges=charges,
        consumption=consumption,
        arrears=arrears,
        penalties=penalties,
        total_due=computed if total_due is None else total_due,
    )


def payment(
    apartments: list[ApartmentLine],
    expenses: list[ExpenseLine],
    *,
    total_general: float | None = None,
    apartment_count: int | None = None,
) -> PaymentList:
    return PaymentList(
        document_id="LP-TEST",
        association_ref="Asociație de test",
        period="2025-11",
        declared_totals=DeclaredTotals(
            total_general=(
                round(sum(line.amount for line in expenses), 2)
                if total_general is None
                else total_general
            ),
            apartment_count=(
                len(apartments) if apartment_count is None else apartment_count
            ),
        ),
        expense_lines=expenses,
        apartment_lines=apartments,
    )

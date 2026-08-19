"""Teste pentru partea deterministă a extractorului.

Nu se face niciun apel de rețea: se testează doar alinierea cheilor de
cheltuială, care este cod obișnuit, nu comportament de model.
"""

from __future__ import annotations

import pytest

from consilium.extractor import align_charge_keys
from consilium.schema import ApartmentLine

CATEGORIES = [
    "Apă rece + canal",
    "Apă caldă menajeră",
    "Energie termică",
    "Salubritate",
    "Întreținere lift",
    "Administrare",
    "Fond reparații",
    "Fond de rulment",
]


def apartment(charges: dict[str, float]) -> ApartmentLine:
    return ApartmentLine(
        apartment_no="1",
        persons=2,
        cota_indiviza=3.5,
        charges=charges,
        arrears=0.0,
        penalties=0.0,
        total_due=0.0,
    )


def test_exact_labels_pass_through():
    line = apartment({category: 1.0 for category in CATEGORIES})
    assert align_charge_keys([line], CATEGORIES) == []
    assert list(line.charges) == CATEGORIES


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Apă caldă", "Apă caldă menajeră"),
        ("Salubri-tate", "Salubritate"),
        ("Adminis- trare", "Administrare"),
        ("APĂ RECE + CANAL", "Apă rece + canal"),
        ("Energie termica", "Energie termică"),
    ],
)
def test_abbreviated_and_hyphenated_headers_are_matched(header, expected):
    line = apartment({header: 42.0})
    assert align_charge_keys([line], CATEGORIES) == []
    assert line.charges == {expected: 42.0}


def test_unknown_column_is_reported_not_forced():
    line = apartment({"Taxă necunoscută": 9.9})
    unresolved = align_charge_keys([line], CATEGORIES)
    assert unresolved == ["apartment_lines[0].charges['Taxă necunoscută']"]
    assert line.charges == {"Taxă necunoscută": 9.9}


def test_ambiguous_prefix_is_reported_not_guessed():
    categories = ["Fond reparații", "Fond rulment"]
    line = apartment({"Fond": 10.0})
    unresolved = align_charge_keys([line], categories)
    assert unresolved == ["apartment_lines[0].charges['Fond']"]
    assert line.charges == {"Fond": 10.0}


def test_values_are_never_altered():
    line = apartment({"Apă caldă": 53.2, "Salubri-tate": 25.0})
    align_charge_keys([line], CATEGORIES)
    assert sorted(line.charges.values()) == [25.0, 53.2]

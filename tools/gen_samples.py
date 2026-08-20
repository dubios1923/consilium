"""Generator de liste de plata sintetice pentru Consilium.

Produce trei PDF-uri in samples/synthetic/ plus expected_findings.json, care
documenteaza fiecare eroare plantata (id, tip, locatie, valoare corecta vs
valoare din document, severitate).

TOATE datele sunt fictive: asociatia, adresa, codul fiscal, furnizorii,
numerele de factura si sumele sunt generate determinist dintr-un seed.
Nu exista nicio corespondenta cu persoane, imobile sau entitati reale.

Rulare:
    python tools/gen_samples.py [--out samples/synthetic]
"""

from __future__ import annotations

import argparse
import io
import json
import random
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

SEED = 20251119
APARTMENT_COUNT = 28
PERIOD = "2025-11"

# Cota legala maxima de penalizare, art. 77 alin. (2) din Legea 196/2018.
PENALTY_RATE_PER_DAY = 0.002
PENALTY_DAYS = 30

FONT_CANDIDATES = [
    ("/usr/share/fonts/liberation-sans-fonts/LiberationSans-Regular.ttf",
     "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
     "/usr/share/fonts/google-noto/NotoSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
     "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf"),
]

FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"


def register_fonts() -> None:
    """Inregistreaza un font TTF cu diacritice romanesti (s/t cu virgula)."""
    global FONT_REGULAR, FONT_BOLD
    for regular, bold in FONT_CANDIDATES:
        if Path(regular).exists() and Path(bold).exists():
            pdfmetrics.registerFont(TTFont("ConsiliumSans", regular))
            pdfmetrics.registerFont(TTFont("ConsiliumSans-Bold", bold))
            FONT_REGULAR, FONT_BOLD = "ConsiliumSans", "ConsiliumSans-Bold"
            return
    raise SystemExit(
        "Niciun font TTF cu diacritice romanesti nu a fost gasit. "
        "Instaleaza liberation-sans-fonts sau google-noto-sans-fonts."
    )


# --------------------------------------------------------------------------
# Model de date
# --------------------------------------------------------------------------

CATEGORIES = [
    # (cheie interna, eticheta din PDF, cheie de repartizare declarata)
    ("apa_rece", "Apă rece + canal", "consum"),
    ("apa_calda", "Apă caldă menajeră", "consum"),
    ("caldura", "Energie termică", "cota_indiviza"),
    ("salubritate", "Salubritate", "persoane"),
    ("lift", "Întreținere lift", "persoane"),
    ("administrare", "Administrare", "egal"),
    ("fond_reparatii", "Fond reparații", "cota_indiviza"),
    ("fond_rulment", "Fond de rulment", "egal"),
]

INVOICE_REFS = {
    "apa_rece": "FF-2025-11-004312",
    "apa_calda": "FF-2025-11-004318",
    "caldura": "FT-2025-11-000877",
    "salubritate": "FS-2025-11-021145",
    "lift": "FL-2025-11-000209",
    "administrare": "CTR-ADM-2024-07",
    "fond_reparatii": "HOT-AG-2025-03",
    "fond_rulment": "HOT-AG-2025-03",
}

PRICE_APA_RECE = 11.50
PRICE_APA_CALDA = 38.00
AMOUNT_CALDURA = 14500.00
PRICE_SALUBRITATE_PERS = 25.00
AMOUNT_LIFT = 1400.00
PRICE_ADMIN_APT = 35.00
AMOUNT_FOND_REPARATII = 2800.00
PRICE_FOND_RULMENT_APT = 20.00


def money(value: float) -> float:
    return round(value + 0.0, 2)


def distribute(amount: float, weights: list[float]) -> list[float]:
    """Imparte `amount` proportional cu `weights`, cu restul pe ultima pozitie.

    Garanteaza ca suma rezultatelor este exact `amount` la doua zecimale.
    """
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("suma ponderilor trebuie sa fie pozitiva")
    parts: list[float] = []
    accumulated = 0.0
    for weight in weights[:-1]:
        part = money(amount * weight / total_weight)
        parts.append(part)
        accumulated = money(accumulated + part)
    parts.append(money(amount - accumulated))
    return parts


def build_apartments(rng: random.Random) -> list[dict[str, Any]]:
    """Construieste fisa de baza a fiecarui apartament (fara sume)."""
    apartments = []
    for number in range(1, APARTMENT_COUNT + 1):
        surface = round(rng.uniform(38.0, 78.0), 2)
        persons = rng.choice([1, 1, 2, 2, 2, 3, 3, 4, 5])
        apartments.append(
            {
                "apartment_no": str(number),
                "surface": surface,
                "persons": persons,
                "m3_apa_rece": round(persons * rng.uniform(1.2, 2.6), 1),
                "m3_apa_calda": round(persons * rng.uniform(0.8, 1.8), 1),
            }
        )

    surfaces = [apartment["surface"] for apartment in apartments]
    cotas = distribute(100.0, surfaces)
    for apartment, cota in zip(apartments, cotas):
        apartment["cota_indiviza"] = cota

    # RNG separat: adaugarea indecsilor nu trebuie sa deplaseze seria de mai sus.
    meter_rng = random.Random(SEED + 1)
    for apartment in apartments:
        rece_vechi = round(meter_rng.uniform(80.0, 640.0), 1)
        calda_vechi = round(meter_rng.uniform(40.0, 380.0), 1)
        apartment["index_rece_vechi"] = rece_vechi
        apartment["index_rece_nou"] = round(rece_vechi + apartment["m3_apa_rece"], 1)
        apartment["index_calda_vechi"] = calda_vechi
        apartment["index_calda_nou"] = round(calda_vechi + apartment["m3_apa_calda"], 1)
    return apartments


def build_expense_lines(apartments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calculeaza suma fiecarei categorii de cheltuiala."""
    total_m3_rece = sum(apartment["m3_apa_rece"] for apartment in apartments)
    total_m3_calda = sum(apartment["m3_apa_calda"] for apartment in apartments)
    total_persons = sum(apartment["persons"] for apartment in apartments)

    amounts = {
        "apa_rece": money(total_m3_rece * PRICE_APA_RECE),
        "apa_calda": money(total_m3_calda * PRICE_APA_CALDA),
        "caldura": AMOUNT_CALDURA,
        "salubritate": money(total_persons * PRICE_SALUBRITATE_PERS),
        "lift": AMOUNT_LIFT,
        "administrare": money(APARTMENT_COUNT * PRICE_ADMIN_APT),
        "fond_reparatii": AMOUNT_FOND_REPARATII,
        "fond_rulment": money(APARTMENT_COUNT * PRICE_FOND_RULMENT_APT),
    }

    return [
        {
            "key": key,
            "category": label,
            "amount": amounts[key],
            "distribution_key": distribution_key,
            "source_invoice_ref": INVOICE_REFS[key],
        }
        for key, label, distribution_key in CATEGORIES
    ]


# Cheia efectiv folosita la repartizare, per categorie, in documentul corect.
DEFAULT_ALLOCATION = {
    "apa_rece": "consum:apa_rece",
    "apa_calda": "consum:apa_calda",
    "caldura": "cota_indiviza",
    "salubritate": "persoane",
    "lift": "persoane",
    "administrare": "egal",
    "fond_reparatii": "cota_indiviza",
    "fond_rulment": "egal",
}

WEIGHT_FUNCTIONS: dict[str, Callable[[dict[str, Any]], float]] = {
    "cota_indiviza": lambda apartment: apartment["cota_indiviza"],
    "persoane": lambda apartment: float(apartment["persons"]),
    "egal": lambda apartment: 1.0,
    "consum:apa_rece": lambda apartment: apartment["m3_apa_rece"],
    "consum:apa_calda": lambda apartment: apartment["m3_apa_calda"],
}

# Restante mostenite din luna precedenta, cu penalizari sub plafonul legal.
BASE_ARREARS = {"5": 412.60, "12": 178.30, "19": 903.15, "24": 256.00}
BASE_PENALTY_RATE = 0.0018  # 0,18%/zi, sub plafonul de 0,2%/zi

PENALTY_SAMPLE_ARREARS = {
    "3": 640.00,
    "5": 412.60,
    "12": 178.30,
    "17": 1120.45,
    "19": 903.15,
    "24": 256.00,
}
# Apartamentele cu penalizari peste plafon si cota abuziva aplicata de administrator.
PENALTY_SAMPLE_RATES = {"3": 0.0050, "12": 0.0035, "17": 0.0042}

# Suma cotelor indivize in sample_penalties (in loc de 100,00%).
BROKEN_COTA_TOTAL = 99.20


def allocate(
    apartments: list[dict[str, Any]],
    expense_lines: list[dict[str, Any]],
    allocation: dict[str, str],
) -> None:
    """Completeaza `charges` pe fiecare apartament conform alocarii date."""
    for apartment in apartments:
        apartment["charges"] = {}
    for line in expense_lines:
        mode = allocation[line["key"]]
        weights = [WEIGHT_FUNCTIONS[mode](apartment) for apartment in apartments]
        for apartment, part in zip(apartments, distribute(line["amount"], weights)):
            apartment["charges"][line["key"]] = part


def apply_arrears(
    apartments: list[dict[str, Any]],
    arrears_map: dict[str, float],
    rate_overrides: dict[str, float],
) -> None:
    for apartment in apartments:
        arrears = arrears_map.get(apartment["apartment_no"], 0.0)
        rate = rate_overrides.get(apartment["apartment_no"], BASE_PENALTY_RATE)
        apartment["arrears"] = money(arrears)
        apartment["penalties"] = money(arrears * rate * PENALTY_DAYS)


def finalize_totals(apartments: list[dict[str, Any]]) -> None:
    for apartment in apartments:
        current = money(sum(apartment["charges"].values()))
        apartment["current_charges_total"] = current
        apartment["total_due"] = money(
            current + apartment["arrears"] + apartment["penalties"]
        )


def penalty_cap(arrears: float) -> float:
    return money(arrears * PENALTY_RATE_PER_DAY * PENALTY_DAYS)


# --------------------------------------------------------------------------
# Antet fictiv
# --------------------------------------------------------------------------

HEADER_META = {
    "association_ref": "Asociația de Proprietari „Zefir 12”",
    "address": "Str. Salcâmului Vechi nr. 12, bl. Z12, sectorul 9, Municipiul Fictiv",
    "cif": "99999999",
    "administrator": "S.C. Administrare Fictivă Exemplu S.R.L.",
    "period_label": "noiembrie 2025",
    "display_date": "05.12.2025",
    "due_date": "20.12.2025",
}


# Anexa de consumuri este publicata doar de unele asociatii. Absenta ei face
# imposibila verificarea pozitiilor repartizate pe consum si trebuie sa apara
# explicit in coverage report, nu sa fie trecuta sub tacere.
SHOW_CONSUMPTION = {"clean": True, "errors": False, "penalties": True}

# Pozitiile repartizate pe consum, care devin neverificabile fara anexa.
CONSUMPTION_DEPENDENT = ["Apă rece + canal", "Apă caldă menajeră"]


def build_document(variant: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Construieste documentul si lista de constatari asteptate.

    variant: "clean" | "errors" | "penalties"
    """
    rng = random.Random(SEED)
    apartments = build_apartments(rng)
    expense_lines = build_expense_lines(apartments)
    allocation = dict(DEFAULT_ALLOCATION)
    arrears_map = dict(BASE_ARREARS)
    rate_overrides: dict[str, float] = {}
    findings: list[dict[str, Any]] = []

    if variant == "penalties":
        # Cotele indivize sunt scalate gresit si nu mai insumeaza 100%.
        for apartment, cota in zip(
            apartments,
            distribute(BROKEN_COTA_TOTAL, [a["surface"] for a in apartments]),
        ):
            apartment["cota_indiviza"] = cota
        arrears_map = dict(PENALTY_SAMPLE_ARREARS)
        rate_overrides = dict(PENALTY_SAMPLE_RATES)

    if variant == "errors":
        # Energia termica este declarata pe cota indiviza, dar repartizata pe consum.
        allocation["caldura"] = "consum:apa_calda"

    allocate(apartments, expense_lines, allocation)
    apply_arrears(apartments, arrears_map, rate_overrides)
    finalize_totals(apartments)

    true_total = money(sum(line["amount"] for line in expense_lines))
    declared_total = true_total

    if variant == "errors":
        # Transcriere gresita a totalului general in antetul tabelului.
        declared_total = money(true_total + 900.00)

    document = {
        "document_id": f"LP-{PERIOD}-Z12-{variant.upper()}",
        "association_ref": HEADER_META["association_ref"],
        "period": PERIOD,
        "declared_totals": {
            "total_general": declared_total,
            "apartment_count": APARTMENT_COUNT,
        },
        "expense_lines": expense_lines,
        "apartment_lines": apartments,
        "_meta": dict(
            HEADER_META,
            variant=variant,
            true_total=true_total,
            show_consumption=SHOW_CONSUMPTION[variant],
        ),
    }

    if variant == "errors":
        findings.append(
            {
                "id": "ERR-001",
                "sample": "sample_errors.pdf",
                "rule_id": "R1",
                "type": "declared_total_mismatch",
                "severity": "high",
                "location": {
                    "section": "Tabel cheltuieli pe categorii",
                    "field": "TOTAL GENERAL",
                },
                "expected_value": true_total,
                "found_value": declared_total,
                "amount_involved": money(declared_total - true_total),
                "legal_reference": None,
                "description": (
                    "Totalul general declarat depaseste suma celor opt pozitii de "
                    "cheltuiala din tabel."
                ),
            }
        )
        deviations = []
        cota_weights = [a["cota_indiviza"] for a in apartments]
        expected_caldura = distribute(AMOUNT_CALDURA, cota_weights)
        for apartment, expected in zip(apartments, expected_caldura):
            found = apartment["charges"]["caldura"]
            if abs(found - expected) > 0.01:
                deviations.append(
                    {
                        "apartment_no": apartment["apartment_no"],
                        "expected_value": expected,
                        "found_value": found,
                        "delta": money(found - expected),
                    }
                )
        findings.append(
            {
                "id": "ERR-002",
                "sample": "sample_errors.pdf",
                "rule_id": "R3",
                "type": "wrong_distribution_key",
                "severity": "high",
                "location": {
                    "section": "Tabel apartamente",
                    "column": "Energie termică",
                    "declared_distribution_key": "cota_indiviza",
                    "actual_distribution_key": "consum",
                },
                "expected_value": "repartizare proportionala cu cota indiviza",
                "found_value": "repartizare proportionala cu consumul de apa calda",
                "amount_involved": money(
                    sum(abs(d["delta"]) for d in deviations) / 2
                ),
                "affected_apartments": [d["apartment_no"] for d in deviations],
                "details": deviations,
                "legal_reference": (
                    "Legea 196/2018 – repartizarea cheltuielilor asociatiei de "
                    "proprietari se face conform cheii stabilite pentru fiecare "
                    "categorie"
                ),
                "description": (
                    "Energia termica este declarata cu cheia „cotă indiviză”, dar "
                    "sumele individuale urmeaza consumul de apa calda."
                ),
            }
        )
        findings.append(
            {
                "id": "ERR-003",
                "sample": "sample_errors.pdf",
                "rule_id": "R5",
                "type": "apartment_sum_mismatch",
                "severity": "high",
                "location": {
                    "section": "Tabel apartamente",
                    "field": "TOTAL coloana „Total lună curentă”",
                },
                "expected_value": declared_total,
                "found_value": money(
                    sum(a["current_charges_total"] for a in apartments)
                ),
                "amount_involved": money(
                    declared_total - sum(a["current_charges_total"] for a in apartments)
                ),
                "legal_reference": None,
                "description": (
                    "Consecinta directa a ERR-001: suma cheltuielilor curente "
                    "repartizate pe apartamente nu atinge totalul general declarat."
                ),
            }
        )

    if not SHOW_CONSUMPTION[variant]:
        findings.append(
            {
                "id": "COV-001",
                "sample": f"sample_{variant}.pdf",
                "rule_id": "R3",
                "type": "unverifiable_distribution",
                "severity": "info",
                "location": {
                    "section": "Document",
                    "field": "anexa de consumuri contorizate",
                },
                "expected_value": "anexă cu consumul individual (mc) pe apartament",
                "found_value": None,
                "amount_involved": money(
                    sum(
                        line["amount"]
                        for line in expense_lines
                        if line["category"] in CONSUMPTION_DEPENDENT
                    )
                ),
                "affected_categories": list(CONSUMPTION_DEPENDENT),
                "legal_reference": None,
                "description": (
                    "Documentul nu publica consumurile individuale, deci "
                    "repartizarea pozitiilor pe consum nu poate fi verificata. "
                    "Proprietarul trebuie sa ceara anexa de consumuri."
                ),
            }
        )

    if variant == "penalties":
        cota_sum = money(sum(a["cota_indiviza"] for a in apartments))
        findings.append(
            {
                "id": "PEN-001",
                "sample": "sample_penalties.pdf",
                "rule_id": "R2",
                "type": "cota_indiviza_sum_mismatch",
                "severity": "high",
                "location": {
                    "section": "Tabel apartamente",
                    "field": "TOTAL coloana „Cotă indiviză %”",
                },
                "expected_value": 100.0,
                "found_value": cota_sum,
                "amount_involved": None,
                "legal_reference": (
                    "Legea 196/2018 – cotele-parti indivize aferente "
                    "apartamentelor insumeaza intreaga proprietate comuna (100%)"
                ),
                "description": (
                    "Suma cotelor indivize declarate este subunitara, deci o parte "
                    "din proprietatea comuna nu are titular de cheltuiala."
                ),
            }
        )
        index = 2
        for apartment in apartments:
            number = apartment["apartment_no"]
            if number not in PENALTY_SAMPLE_RATES:
                continue
            cap = penalty_cap(apartment["arrears"])
            findings.append(
                {
                    "id": f"PEN-{index:03d}",
                    "sample": "sample_penalties.pdf",
                    "rule_id": "R4",
                    "type": "penalty_over_legal_cap",
                    "severity": "high",
                    "location": {
                        "section": "Tabel apartamente",
                        "apartment_no": number,
                        "column": "Penalizări",
                    },
                    "expected_value": cap,
                    "found_value": apartment["penalties"],
                    "amount_involved": money(apartment["penalties"] - cap),
                    "applied_rate_per_day": PENALTY_SAMPLE_RATES[number],
                    "legal_cap_rate_per_day": PENALTY_RATE_PER_DAY,
                    "days": PENALTY_DAYS,
                    "arrears": apartment["arrears"],
                    "legal_reference": "Legea 196/2018, art. 77 alin. (2)",
                    "description": (
                        f"Penalizarea aplicata apartamentului {number} corespunde "
                        f"unei cote de {PENALTY_SAMPLE_RATES[number] * 100:.2f}%/zi, "
                        f"peste plafonul legal de "
                        f"{PENALTY_RATE_PER_DAY * 100:.1f}%/zi."
                    ),
                }
            )
            index += 1

    return document, findings


# --------------------------------------------------------------------------
# Randare PDF
# --------------------------------------------------------------------------

KEY_LABELS = {
    "cota_indiviza": "cotă indiviză",
    "persoane": "număr persoane",
    "consum": "consum contorizat",
    "egal": "părți egale",
}

INK = colors.HexColor("#1a1a1a")
RULE = colors.HexColor("#8a8a8a")
BAND = colors.HexColor("#eceff3")
HEAD = colors.HexColor("#d6dce5")


def fmt(value: float) -> str:
    """Formateaza o suma in stil romanesc: 1.234,56"""
    text = f"{value:,.2f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def styles() -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle(
            "title", fontName=FONT_BOLD, fontSize=13, leading=16, textColor=INK
        ),
        "sub": ParagraphStyle(
            "sub", fontName=FONT_REGULAR, fontSize=8, leading=11, textColor=INK
        ),
        "section": ParagraphStyle(
            "section", fontName=FONT_BOLD, fontSize=9, leading=12, textColor=INK,
            spaceBefore=6, spaceAfter=3,
        ),
        "th": ParagraphStyle(
            "th", fontName=FONT_BOLD, fontSize=5.4, leading=6.4, textColor=INK,
            alignment=1,
        ),
        "note": ParagraphStyle(
            "note", fontName=FONT_REGULAR, fontSize=6.5, leading=8.5,
            textColor=colors.HexColor("#4a4a4a"),
        ),
    }


def expense_table(document: dict[str, Any], style: dict[str, ParagraphStyle]) -> Table:
    rows = [["Categorie de cheltuială", "Sumă (lei)", "Cheie de repartizare",
             "Document justificativ"]]
    for line in document["expense_lines"]:
        rows.append(
            [
                line["category"],
                fmt(line["amount"]),
                KEY_LABELS[line["distribution_key"]],
                line["source_invoice_ref"],
            ]
        )
    rows.append(
        ["TOTAL GENERAL", fmt(document["declared_totals"]["total_general"]), "", ""]
    )

    table = Table(rows, colWidths=[70 * mm, 35 * mm, 55 * mm, 60 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), FONT_REGULAR),
                ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
                ("FONTNAME", (0, -1), (-1, -1), FONT_BOLD),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("BACKGROUND", (0, 0), (-1, 0), HEAD),
                ("BACKGROUND", (0, -1), (-1, -1), BAND),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.35, RULE),
                ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ]
        )
    )
    return table


APARTMENT_HEADERS = [
    "Ap.", "Nr.<br/>pers.", "Cotă<br/>indiviză<br/>%", "Apă rece<br/>+ canal",
    "Apă caldă", "Energie<br/>termică", "Salubri-<br/>tate", "Între-<br/>ținere<br/>lift",
    "Adminis-<br/>trare", "Fond<br/>reparații", "Fond de<br/>rulment",
    "Total lună<br/>curentă", "Restanțe", "Penali-<br/>zări", "TOTAL<br/>DE PLATĂ",
]

APARTMENT_WIDTHS = [10, 10, 14, 18, 18, 18, 18, 18, 18, 18, 18, 24, 20, 22, 26]


def apartment_table(document: dict[str, Any], style: dict[str, ParagraphStyle]) -> Table:
    rows = [[Paragraph(text, style["th"]) for text in APARTMENT_HEADERS]]
    keys = [key for key, _, _ in CATEGORIES]

    for apartment in document["apartment_lines"]:
        row = [
            apartment["apartment_no"],
            str(apartment["persons"]),
            f"{apartment['cota_indiviza']:.2f}".replace(".", ","),
        ]
        row += [fmt(apartment["charges"][key]) for key in keys]
        row += [
            fmt(apartment["current_charges_total"]),
            fmt(apartment["arrears"]) if apartment["arrears"] else "0,00",
            fmt(apartment["penalties"]) if apartment["penalties"] else "0,00",
            fmt(apartment["total_due"]),
        ]
        rows.append(row)

    apartments = document["apartment_lines"]
    total_row = [
        "TOTAL",
        str(sum(a["persons"] for a in apartments)),
        f"{sum(a['cota_indiviza'] for a in apartments):.2f}".replace(".", ","),
    ]
    total_row += [
        fmt(money(sum(a["charges"][key] for a in apartments))) for key in keys
    ]
    total_row += [
        fmt(money(sum(a["current_charges_total"] for a in apartments))),
        fmt(money(sum(a["arrears"] for a in apartments))),
        fmt(money(sum(a["penalties"] for a in apartments))),
        fmt(money(sum(a["total_due"] for a in apartments))),
    ]
    rows.append(total_row)

    table = Table(
        rows,
        colWidths=[width * mm for width in APARTMENT_WIDTHS],
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 1), (-1, -1), FONT_REGULAR),
                ("FONTNAME", (0, -1), (-1, -1), FONT_BOLD),
                ("FONTSIZE", (0, 1), (-1, -1), 6),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (0, 0), (-1, 0), HEAD),
                ("BACKGROUND", (0, -1), (-1, -1), BAND),
                ("GRID", (0, 0), (-1, -1), 0.3, RULE),
                ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                ("TOPPADDING", (0, 1), (-1, -1), 1.6),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 1.6),
                ("ROWBACKGROUNDS", (0, 1), (-1, -2),
                 [colors.white, colors.HexColor("#f6f7f9")]),
            ]
        )
    )
    return table


CONSUMPTION_HEADERS = [
    "Ap.", "Apă rece<br/>index vechi", "Apă rece<br/>index nou",
    "Apă rece<br/>consum (mc)", "Apă caldă<br/>index vechi",
    "Apă caldă<br/>index nou", "Apă caldă<br/>consum (mc)",
]

CONSUMPTION_WIDTHS = [14, 30, 30, 30, 30, 30, 30]


def consumption_table(
    document: dict[str, Any], style: dict[str, ParagraphStyle]
) -> Table:
    """Anexa cu indecsii de contor si consumul repartizabil pe apartament."""
    rows = [[Paragraph(text, style["th"]) for text in CONSUMPTION_HEADERS]]
    for apartment in document["apartment_lines"]:
        rows.append(
            [
                apartment["apartment_no"],
                f"{apartment['index_rece_vechi']:.1f}".replace(".", ","),
                f"{apartment['index_rece_nou']:.1f}".replace(".", ","),
                f"{apartment['m3_apa_rece']:.1f}".replace(".", ","),
                f"{apartment['index_calda_vechi']:.1f}".replace(".", ","),
                f"{apartment['index_calda_nou']:.1f}".replace(".", ","),
                f"{apartment['m3_apa_calda']:.1f}".replace(".", ","),
            ]
        )
    apartments = document["apartment_lines"]
    rows.append(
        [
            "TOTAL", "", "",
            f"{sum(a['m3_apa_rece'] for a in apartments):.1f}".replace(".", ","),
            "", "",
            f"{sum(a['m3_apa_calda'] for a in apartments):.1f}".replace(".", ","),
        ]
    )

    table = Table(
        rows,
        colWidths=[width * mm for width in CONSUMPTION_WIDTHS],
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 1), (-1, -1), FONT_REGULAR),
                ("FONTNAME", (0, -1), (-1, -1), FONT_BOLD),
                ("FONTSIZE", (0, 1), (-1, -1), 6.5),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (0, 0), (-1, 0), HEAD),
                ("BACKGROUND", (0, -1), (-1, -1), BAND),
                ("GRID", (0, 0), (-1, -1), 0.3, RULE),
                ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                ("TOPPADDING", (0, 1), (-1, -1), 1.8),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 1.8),
                ("ROWBACKGROUNDS", (0, 1), (-1, -2),
                 [colors.white, colors.HexColor("#f6f7f9")]),
            ]
        )
    )
    return table


def render_pdf(document: dict[str, Any], path: Path) -> None:
    style = styles()
    meta = document["_meta"]
    doc = SimpleDocTemplate(
        str(path),
        pagesize=landscape(A4),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=f"Listă de plată {meta['period_label']}",
        author="Consilium – generator de date sintetice",
        # Fara marca de timp: acelasi seed trebuie sa dea acelasi PDF, octet cu octet.
        invariant=1,
    )

    story: list[Any] = [
        Paragraph(
            f"LISTĂ DE PLATĂ – {meta['period_label'].upper()}", style["title"]
        ),
        Spacer(1, 3),
        Paragraph(
            f"{document['association_ref']} &nbsp;|&nbsp; {meta['address']} "
            f"&nbsp;|&nbsp; CIF: {meta['cif']}",
            style["sub"],
        ),
        Paragraph(
            f"Administrator: {meta['administrator']} &nbsp;|&nbsp; "
            f"Document: {document['document_id']} &nbsp;|&nbsp; "
            f"Perioada: {document['period']} &nbsp;|&nbsp; "
            f"Număr apartamente: {document['declared_totals']['apartment_count']}",
            style["sub"],
        ),
        Paragraph(
            f"Data afișării: {meta['display_date']} &nbsp;|&nbsp; "
            f"Termen de plată: {meta['due_date']} &nbsp;|&nbsp; "
            f"Penalizările din prezenta listă sunt calculate pentru "
            f"{PENALTY_DAYS} de zile de întârziere.",
            style["sub"],
        ),
        Paragraph("1. Cheltuieli pe categorii", style["section"]),
        expense_table(document, style),
        Paragraph("2. Repartizarea pe apartamente", style["section"]),
        apartment_table(document, style),
    ]

    if meta["show_consumption"]:
        story += [
            Paragraph(
                "3. Anexă – consumuri contorizate individual", style["section"]
            ),
            consumption_table(document, style),
        ]

    story += [
        Spacer(1, 5),
        Paragraph(
            "DOCUMENT FICTIV, GENERAT AUTOMAT PENTRU TESTAREA APLICAȚIEI CONSILIUM. "
            "Asociația, adresa, codul fiscal, furnizorii, numerele de factură și "
            "toate sumele sunt inventate și nu corespund niciunei entități reale.",
            style["note"],
        ),
    ]
    doc.build(story)


# --------------------------------------------------------------------------
# Layout alternativ
#
# Aceleasi date, acelasi seed, aceleasi erori plantate, dar alt ambalaj. Exista
# sa se poata masura cat de generala e extractia: un layout inventat de acelasi
# generator care produce si sample-urile de baza nu dovedeste nimic daca arata
# la fel. Aici se schimba tot ce se schimba intre doua programe de administrare
# reale: denumirile din antet, ordinea coloanelor, abrevierile in loc de nume
# complete, si ordinea sectiunilor (apartamentele inaintea cheltuielilor).
# --------------------------------------------------------------------------

ALT_HEADER_META = {
    "title": "SITUAȚIE LUNARĂ DE REPARTIZARE A CHELTUIELILOR",
    "org_label": "Unitatea",
    "period_label": "Luna de referință",
    "count_label": "Nr. unități locative",
    "posting_label": "Afișat în data de",
    "due_label": "Scadență",
    "doc_label": "Nr. document",
}

# Abrevierile din antetul de coloana, in loc de denumirile complete. Etichetele
# canonice raman cele din tabelul de cheltuieli, ca in orice document real.
ALT_COLUMN_ABBREVIATIONS = {
    "apa_rece": "A.R.+can.",
    "apa_calda": "A.C.M.",
    "caldura": "En.term.",
    "salubritate": "Salubr.",
    "lift": "Lift",
    "administrare": "Admin.",
    "fond_reparatii": "F.rep.",
    "fond_rulment": "F.rulm.",
}

# Ordinea coloanelor de cheltuiala, alta decat in layout-ul de baza.
ALT_CATEGORY_ORDER = [
    "administrare",
    "fond_rulment",
    "fond_reparatii",
    "lift",
    "salubritate",
    "caldura",
    "apa_calda",
    "apa_rece",
]


def alt_apartment_table(
    document: dict[str, Any], style: dict[str, ParagraphStyle]
) -> Table:
    """Tabelul de apartamente cu ordinea si abrevierile layout-ului alternativ."""
    headers = ["Unit.", "Cotă%", "Pers."]
    headers += [ALT_COLUMN_ABBREVIATIONS[key] for key in ALT_CATEGORY_ORDER]
    headers += ["Restanță", "Penaliz.", "Curent", "DE ACHITAT"]
    rows: list[list[Any]] = [
        [Paragraph(text, style["th"]) for text in headers]
    ]

    for apartment in document["apartment_lines"]:
        row = [
            apartment["apartment_no"],
            f"{apartment['cota_indiviza']:.2f}".replace(".", ","),
            str(apartment["persons"]),
        ]
        row += [fmt(apartment["charges"][key]) for key in ALT_CATEGORY_ORDER]
        row += [
            fmt(apartment["arrears"]),
            fmt(apartment["penalties"]),
            fmt(apartment["current_charges_total"]),
            fmt(apartment["total_due"]),
        ]
        rows.append(row)

    apartments = document["apartment_lines"]
    total_row = [
        "TOTAL",
        f"{sum(a['cota_indiviza'] for a in apartments):.2f}".replace(".", ","),
        str(sum(a["persons"] for a in apartments)),
    ]
    total_row += [
        fmt(money(sum(a["charges"][key] for a in apartments)))
        for key in ALT_CATEGORY_ORDER
    ]
    total_row += [
        fmt(money(sum(a["arrears"] for a in apartments))),
        fmt(money(sum(a["penalties"] for a in apartments))),
        fmt(money(sum(a["current_charges_total"] for a in apartments))),
        fmt(money(sum(a["total_due"] for a in apartments))),
    ]
    rows.append(total_row)

    widths = [12, 14, 11] + [20] * 8 + [21, 20, 23, 26]
    table = Table(
        rows,
        colWidths=[width * mm for width in widths],
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 1), (-1, -1), FONT_REGULAR),
                ("FONTNAME", (0, -1), (-1, -1), FONT_BOLD),
                ("FONTSIZE", (0, 1), (-1, -1), 6),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (0, 0), (-1, 0), HEAD),
                ("BACKGROUND", (0, -1), (-1, -1), BAND),
                ("GRID", (0, 0), (-1, -1), 0.3, RULE),
                ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                ("TOPPADDING", (0, 1), (-1, -1), 1.6),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 1.6),
            ]
        )
    )
    return table


def alt_expense_table(
    document: dict[str, Any], style: dict[str, ParagraphStyle]
) -> Table:
    """Tabelul de cheltuieli, cu coloanele in alta ordine si alte denumiri."""
    by_key = {line["key"]: line for line in document["expense_lines"]}
    rows = [["Mod de repartizare", "Denumire cheltuială", "Act justificativ",
             "Valoare (RON)"]]
    for key in ALT_CATEGORY_ORDER:
        line = by_key[key]
        rows.append(
            [
                KEY_LABELS[line["distribution_key"]],
                line["category"],
                line["source_invoice_ref"],
                fmt(line["amount"]),
            ]
        )
    rows.append(
        ["", "TOTAL CHELTUIELI", "",
         fmt(document["declared_totals"]["total_general"])]
    )

    table = Table(
        rows, colWidths=[45 * mm, 70 * mm, 55 * mm, 35 * mm], hAlign="LEFT"
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), FONT_REGULAR),
                ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
                ("FONTNAME", (0, -1), (-1, -1), FONT_BOLD),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("BACKGROUND", (0, 0), (-1, 0), HEAD),
                ("BACKGROUND", (0, -1), (-1, -1), BAND),
                ("ALIGN", (3, 0), (3, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.35, RULE),
                ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ]
        )
    )
    return table


def render_alt_pdf(document: dict[str, Any], path: Path) -> None:
    """Randeaza layout-ul alternativ: apartamentele intai, cheltuielile dupa."""
    style = styles()
    meta = document["_meta"]
    doc = SimpleDocTemplate(
        str(path),
        pagesize=landscape(A4),
        leftMargin=8 * mm,
        rightMargin=8 * mm,
        topMargin=9 * mm,
        bottomMargin=9 * mm,
        title=ALT_HEADER_META["title"],
        author="Consilium – generator de date sintetice",
        invariant=1,
    )

    header = (
        f"{ALT_HEADER_META['org_label']}: {document['association_ref']} "
        f"&nbsp;·&nbsp; {ALT_HEADER_META['doc_label']}: {document['document_id']} "
        f"&nbsp;·&nbsp; {ALT_HEADER_META['period_label']}: {document['period']}"
    )
    header2 = (
        f"{ALT_HEADER_META['count_label']}: "
        f"{document['declared_totals']['apartment_count']} &nbsp;·&nbsp; "
        f"{ALT_HEADER_META['posting_label']}: {meta['display_date']} "
        f"&nbsp;·&nbsp; {ALT_HEADER_META['due_label']}: {meta['due_date']} "
        f"&nbsp;·&nbsp; {meta['address']} &nbsp;·&nbsp; CIF {meta['cif']}"
    )

    story: list[Any] = [
        Paragraph(ALT_HEADER_META["title"], style["title"]),
        Paragraph(header, style["sub"]),
        Paragraph(header2, style["sub"]),
        Paragraph(
            f"Penalizările din prezenta situație sunt calculate pentru "
            f"{PENALTY_DAYS} de zile de întârziere.",
            style["sub"],
        ),
        Paragraph("A. Defalcare pe unități locative", style["section"]),
        alt_apartment_table(document, style),
        PageBreak(),
        Paragraph("B. Centralizator cheltuieli", style["section"]),
        alt_expense_table(document, style),
    ]

    if meta["show_consumption"]:
        story += [
            Paragraph("C. Contorizări individuale", style["section"]),
            consumption_table(document, style),
        ]

    story += [
        Spacer(1, 5),
        Paragraph(
            "DOCUMENT FICTIV, GENERAT AUTOMAT PENTRU TESTAREA APLICAȚIEI CONSILIUM. "
            "Asociația, adresa, codul fiscal, furnizorii, numerele de factură și "
            "toate sumele sunt inventate și nu corespund niciunei entități reale.",
            style["note"],
        ),
    ]
    doc.build(story)


# --------------------------------------------------------------------------
# Varianta scanata
#
# Aceleasi documente, trecute printr-un lant de degradare care imita un scan de
# birou: rasterizare, inclinare, zgomot de senzor si compresie JPEG. Nu schimba
# niciun numar tiparit, doar cat de sigur poate fi citit. Lantul este determinist:
# acelasi seed produce acelasi PDF, octet cu octet.
# --------------------------------------------------------------------------

SCAN_DPI = 150
SCAN_MAX_SKEW_DEGREES = 1.5
SCAN_NOISE_SIGMA = 4.0
SCAN_JPEG_QUALITY = 75
SCAN_SEED_OFFSET = 7000


def _rasterize(source: Path, work_dir: Path) -> list[Path]:
    """Rasterizeaza PDF-ul la SCAN_DPI folosind pdftoppm."""
    prefix = work_dir / "page"
    result = subprocess.run(
        ["pdftoppm", "-r", str(SCAN_DPI), "-png", str(source), str(prefix)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"pdftoppm a esuat pentru {source}: {result.stderr.strip()}"
        )
    pages = sorted(work_dir.glob("page-*.png"))
    if not pages:
        raise SystemExit(f"pdftoppm nu a produs nicio pagina pentru {source}")
    return pages


def _degrade_page(png_path: Path, rng: np.random.Generator) -> bytes:
    """Inclina, adauga zgomot si comprima o pagina. Intoarce octeti JPEG."""
    with Image.open(png_path) as raw:
        page = raw.convert("L")

    angle = float(rng.uniform(-SCAN_MAX_SKEW_DEGREES, SCAN_MAX_SKEW_DEGREES))
    page = page.rotate(
        angle, resample=Image.BICUBIC, expand=False, fillcolor=255
    )

    pixels = np.asarray(page, dtype=np.float32)
    pixels += rng.normal(0.0, SCAN_NOISE_SIGMA, pixels.shape)
    page = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), mode="L")

    buffer = io.BytesIO()
    page.save(buffer, format="JPEG", quality=SCAN_JPEG_QUALITY, optimize=False)
    return buffer.getvalue()


def make_scanned(source: Path, target: Path, seed: int) -> None:
    """Produce varianta scanata a unui PDF deja generat."""
    rng = np.random.default_rng(seed)
    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        pages = _rasterize(source, work_dir)

        canvas_obj: canvas.Canvas | None = None
        for page_path in pages:
            with Image.open(page_path) as probe:
                width_px, height_px = probe.size
            width_pt = width_px * 72.0 / SCAN_DPI
            height_pt = height_px * 72.0 / SCAN_DPI

            jpeg = _degrade_page(page_path, rng)

            if canvas_obj is None:
                canvas_obj = canvas.Canvas(
                    str(target), pagesize=(width_pt, height_pt), invariant=1
                )
            else:
                canvas_obj.setPageSize((width_pt, height_pt))
            canvas_obj.drawImage(
                ImageReader(io.BytesIO(jpeg)), 0, 0, width_pt, height_pt
            )
            canvas_obj.showPage()

        assert canvas_obj is not None
        canvas_obj.setTitle(f"Scan {source.stem}")
        canvas_obj.setAuthor("Consilium – generator de date sintetice")
        canvas_obj.save()


# --------------------------------------------------------------------------
# Document care NU este lista de plata
#
# Serveste gate-ului de intrare: fara un negativ, triajul nu poate fi demonstrat
# decat pe un document real al cuiva. Contine exact tiparele care trebuie sa NU
# declanseze pipeline-ul: hotarare de adunare generala, fara tabel de
# repartizare pe apartamente.
# --------------------------------------------------------------------------

AGA_RESOLUTIONS = [
    (
        "Aprobarea execuției bugetare pentru exercițiul financiar încheiat, "
        "prezentată de comitetul executiv."
    ),
    (
        "Aprobarea bugetului de venituri și cheltuieli pentru anul în curs, cu "
        "menținerea cotei de contribuție la fondul de reparații."
    ),
    (
        "Mandatarea președintelui asociației pentru semnarea contractului de "
        "service al ascensorului, pe o durată de 24 de luni."
    ),
    (
        "Aprobarea constituirii unui fond de reparații suplimentar pentru "
        "reabilitarea instalației de distribuție a apei calde."
    ),
    (
        "Stabilirea programului de audiențe al administratorului: marți și joi, "
        "între orele 17:00 și 19:00."
    ),
]


def render_not_a_payment_list(path: Path) -> None:
    """Randeaza o hotarare AGA fictiva, ca negativ pentru gate-ul de intrare."""
    style = styles()
    body = ParagraphStyle(
        "aga_body", fontName=FONT_REGULAR, fontSize=10.5, leading=15.5,
        alignment=4, spaceAfter=8,
    )
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=25 * mm, rightMargin=25 * mm,
        topMargin=22 * mm, bottomMargin=20 * mm,
        title="Hotărâre a adunării generale",
        author="Consilium – generator de date sintetice",
        invariant=1,
    )
    items = "".join(
        f"<br/><b>Art. {index}.</b> {text}"
        for index, text in enumerate(AGA_RESOLUTIONS, start=1)
    )
    story: list[Any] = [
        Paragraph(HEADER_META["association_ref"], style["sub"]),
        Paragraph(f"{HEADER_META['address']} · CIF {HEADER_META['cif']}", style["sub"]),
        Spacer(1, 16),
        Paragraph("HOTĂRÂREA nr. 4 / 18.11.2025", style["title"]),
        Paragraph("a adunării generale a proprietarilor", style["section"]),
        Paragraph(
            "Adunarea generală a proprietarilor, întrunită statutar în data de "
            "18.11.2025, la sediul asociației, în prezența a 21 din cei 28 de "
            "proprietari, cu respectarea cvorumului prevăzut de Legea nr. "
            "196/2018, adoptă următoarele:",
            body,
        ),
        Paragraph(items, body),
        Spacer(1, 14),
        Paragraph(
            "Prezenta hotărâre a fost adoptată cu 19 voturi „pentru”, 2 voturi "
            "„împotrivă” și nicio abținere, și se afișează la avizierul "
            "asociației.",
            body,
        ),
        Spacer(1, 26),
        Paragraph("Președinte al asociației", style["sub"]),
        Paragraph("Secretar de ședință", style["sub"]),
        Spacer(1, 18),
        Paragraph(
            "DOCUMENT FICTIV, GENERAT AUTOMAT PENTRU TESTAREA APLICAȚIEI CONSILIUM.",
            style["note"],
        ),
    ]
    doc.build(story)


# --------------------------------------------------------------------------
# Intrare
# --------------------------------------------------------------------------

VARIANTS = [
    ("clean", "sample_clean.pdf"),
    ("errors", "sample_errors.pdf"),
    ("penalties", "sample_penalties.pdf"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="samples/synthetic",
        help="directorul de iesire (implicit: samples/synthetic)",
    )
    parser.add_argument(
        "--skip-alt-layout",
        action="store_true",
        help="nu genera varianta cu layout alternativ",
    )
    parser.add_argument(
        "--skip-scanned",
        action="store_true",
        help="nu genera variantele scanate (necesita pdftoppm, Pillow, numpy)",
    )
    args = parser.parse_args()

    register_fonts()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_findings: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}

    for index, (variant, filename) in enumerate(VARIANTS):
        document, findings = build_document(variant)
        render_pdf(document, out_dir / filename)
        all_findings.extend(findings)

        scanned_name: str | None = None
        if not args.skip_scanned:
            scanned_name = filename.replace(".pdf", "_scanned.pdf")
            make_scanned(
                out_dir / filename,
                out_dir / scanned_name,
                SEED + SCAN_SEED_OFFSET + index,
            )

        verifiable = [
            line["category"]
            for line in document["expense_lines"]
            if document["_meta"]["show_consumption"]
            or line["category"] not in CONSUMPTION_DEPENDENT
        ]
        summary[filename] = {
            "variant": variant,
            "consumption_annex_present": document["_meta"]["show_consumption"],
            "expected_coverage": {
                "expense_lines_total": len(document["expense_lines"]),
                "expense_lines_verifiable": len(verifiable),
                "expense_lines_unverifiable": [
                    line["category"]
                    for line in document["expense_lines"]
                    if line["category"] not in verifiable
                ],
            },
            "expected_finding_count": len(findings),
            "expected_finding_ids": [f["id"] for f in findings],
            "declared_total_general": document["declared_totals"]["total_general"],
            "computed_expense_sum": document["_meta"]["true_total"],
        }
        if scanned_name:
            summary[scanned_name] = dict(
                summary[filename],
                derived_from=filename,
                degradation={
                    "dpi": SCAN_DPI,
                    "max_skew_degrees": SCAN_MAX_SKEW_DEGREES,
                    "gaussian_noise_sigma": SCAN_NOISE_SIGMA,
                    "jpeg_quality": SCAN_JPEG_QUALITY,
                },
                note=(
                    "Aceleasi constatari plantate ca in documentul sursa; "
                    "difera doar calitatea imaginii."
                ),
            )

        print(f"scris {out_dir / filename}  ({len(findings)} constatări plantate)")
        if scanned_name:
            print(f"scris {out_dir / scanned_name}  (variantă scanată)")

        if not args.skip_alt_layout:
            alt_name = filename.replace(".pdf", "_alt.pdf")
            render_alt_pdf(document, out_dir / alt_name)
            summary[alt_name] = dict(
                summary[filename],
                derived_from=filename,
                layout="alternativ",
                note=(
                    "Aceleași date și aceleași constatări plantate, alt layout: "
                    "alte denumiri de antet, altă ordine a coloanelor, abrevieri, "
                    "tabelul de cheltuieli după cel de apartamente, două pagini."
                ),
            )
            print(f"scris {out_dir / alt_name}  (layout alternativ)")

    payload = {
        "generator": "tools/gen_samples.py",
        "seed": SEED,
        "period": PERIOD,
        "apartment_count": APARTMENT_COUNT,
        "disclaimer": (
            "Date complet fictive. Nicio corespondență cu persoane, imobile, "
            "asociații sau documente reale."
        ),
        "penalty_model": {
            "legal_cap_rate_per_day": PENALTY_RATE_PER_DAY,
            "days": PENALTY_DAYS,
            "legal_reference": "Legea 196/2018, art. 77 alin. (2)",
        },
        "samples": summary,
        "findings": all_findings,
    }

    negative = out_dir / "sample_not_a_payment_list.pdf"
    render_not_a_payment_list(negative)
    summary[negative.name] = {
        "variant": "negativ",
        "note": (
            "Nu este listă de plată. Serveşte gate-ului de intrare: trebuie "
            "respins la triaj, fără să pornească extracția."
        ),
        "expected_triage": "rejected",
    }
    print(f"scris {negative}  (negativ pentru gate-ul de intrare)")

    findings_path = out_dir / "expected_findings.json"
    findings_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"scris {findings_path}  ({len(all_findings)} constatări în total)")


if __name__ == "__main__":
    main()

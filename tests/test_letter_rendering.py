"""Randarea scrisorii: diacritice, tipografie, structură.

Verificarea diacriticelor se face programatic, extrăgând textul din PDF-ul
generat, nu privind imaginea. Un PDF randat cu Helvetica arată plauzibil pe
ecran la prima vedere, dar „Către” iese din el ca „C?tre”: exact eroarea care a
ajuns în producție și pe care inspecția vizuală a ratat-o.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from consilium.drafter import (
    FONT_BOLD,
    FONT_REGULAR,
    REQUIRED_GLYPHS,
    LetterDraft,
    _missing_glyphs,
    _register_fonts,
    render_letter_pdf,
)
from consilium.reconciler import Finding

pdftotext = shutil.which("pdftotext")


@pytest.fixture
def draft() -> LetterDraft:
    findings = [
        Finding(
            rule_id="R4",
            severity="high",
            finding_type="penalty_over_legal_cap",
            message="mesaj",
            apartment_no="17",
            amount_involved=73.95,
        ),
        Finding(
            rule_id="R2",
            severity="high",
            finding_type="cota_indiviza_sum_mismatch",
            message="mesaj",
        ),
    ]
    return LetterDraft(
        subject="Solicitare privind lista de plată",
        opening="Subsemnatul, proprietar în cadrul asociației, vă adresez prezenta.",
        paragraphs=[
            (findings[0], "Penalizare peste plafonul legal, față de cota admisă."),
            (findings[1], "Suma cotelor indivize nu însumează întregul."),
        ],
        coverage_paragraph="Nu s-au putut verifica toate pozițiile de cheltuială.",
        requested_documents=[
            "Anexa cu consumurile individuale contorizate.",
            "Facturile furnizorilor pentru fiecare poziție.",
        ],
        deadline_paragraph="Vă solicit răspunsul în termenul prevăzut de lege.",
        closing="Cu stimă,",
        association_ref="Asociația de Proprietari „Zefir 12”",
        period="2025-11",
        document_id="LP-2025-11-Z12",
        audit_id="aud-test",
        title="CERERE DE COMUNICARE A DOCUMENTELOR",
    )


def extract_text(pdf: Path) -> str:
    result = subprocess.run(
        [pdftotext, "-layout", str(pdf), "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


# --------------------------------------------------------------------------
# Fontul
# --------------------------------------------------------------------------


def test_registered_font_is_not_helvetica():
    """Helvetica nu are ă, â, î, ș, ț. Fallback-ul pe ea era bug-ul."""
    regular, bold = _register_fonts()
    assert regular == FONT_REGULAR
    assert bold == FONT_BOLD
    assert "Helvetica" not in (regular, bold)


def test_registered_font_covers_every_romanian_glyph():
    regular, _ = _register_fonts()
    assert _missing_glyphs(regular) == ""


def test_required_glyphs_include_comma_below_forms():
    """ș și ț cu virgulă (U+0219/U+021B) nu sunt în Latin-1 și se ratează ușor."""
    assert "ș" in REQUIRED_GLYPHS
    assert "ț" in REQUIRED_GLYPHS


# --------------------------------------------------------------------------
# Diacriticele în PDF-ul generat
# --------------------------------------------------------------------------


@pytest.mark.skipif(pdftotext is None, reason="pdftotext indisponibil")
def test_diacritics_survive_the_round_trip(draft):
    with tempfile.TemporaryDirectory() as tmp:
        pdf = render_letter_pdf(draft, Path(tmp) / "letter.pdf")
        text = extract_text(pdf)
    assert "Către" in text
    assert "Constatări" in text
    assert "Verificări care nu au putut fi efectuate" in text
    assert "Documente solicitate" in text


@pytest.mark.skipif(pdftotext is None, reason="pdftotext indisponibil")
@pytest.mark.parametrize("glyph", list("ăâîșțĂÂÎȘȚ"))
def test_every_glyph_is_extractable(draft, glyph):
    draft.opening = f"Text de control: {glyph}"
    with tempfile.TemporaryDirectory() as tmp:
        pdf = render_letter_pdf(draft, Path(tmp) / "letter.pdf")
        text = extract_text(pdf)
    assert glyph in text


@pytest.mark.skipif(pdftotext is None, reason="pdftotext indisponibil")
def test_no_replacement_characters_in_output(draft):
    with tempfile.TemporaryDirectory() as tmp:
        pdf = render_letter_pdf(draft, Path(tmp) / "letter.pdf")
        text = extract_text(pdf)
    for broken in ("�", "▖", "?tre", "constat?ri"):
        assert broken not in text


# --------------------------------------------------------------------------
# Tipografia
# --------------------------------------------------------------------------


@pytest.mark.skipif(pdftotext is None, reason="pdftotext indisponibil")
def test_section_heading_is_not_glued_to_the_list_numbering(draft):
    """„Documente solicitate1” era titlul lipit de primul număr al listei."""
    with tempfile.TemporaryDirectory() as tmp:
        pdf = render_letter_pdf(draft, Path(tmp) / "letter.pdf")
        text = extract_text(pdf)
    assert "Documente solicitate1" not in text
    assert "solicitate1" not in text


@pytest.mark.skipif(pdftotext is None, reason="pdftotext indisponibil")
def test_every_requested_document_is_numbered(draft):
    with tempfile.TemporaryDirectory() as tmp:
        pdf = render_letter_pdf(draft, Path(tmp) / "letter.pdf")
        lines = extract_text(pdf).splitlines()
    numbered = [line.strip() for line in lines if line.strip().startswith(("1", "2"))]
    assert any("Anexa cu consumurile" in line for line in numbered)
    assert any("Facturile furnizorilor" in line for line in numbered)


@pytest.mark.skipif(pdftotext is None, reason="pdftotext indisponibil")
def test_each_finding_has_rule_and_amount_on_separate_lines(draft):
    with tempfile.TemporaryDirectory() as tmp:
        pdf = render_letter_pdf(draft, Path(tmp) / "letter.pdf")
        lines = [line.strip() for line in extract_text(pdf).splitlines()]
    label = next(i for i, line in enumerate(lines) if line.startswith("[R4"))
    assert "Sumă implicată" not in lines[label]
    assert lines[label + 1].startswith("Sumă implicată: 73,95 lei")


@pytest.mark.skipif(pdftotext is None, reason="pdftotext indisponibil")
def test_finding_without_amount_has_no_amount_line(draft):
    with tempfile.TemporaryDirectory() as tmp:
        pdf = render_letter_pdf(draft, Path(tmp) / "letter.pdf")
        lines = [line.strip() for line in extract_text(pdf).splitlines()]
    label = next(i for i, line in enumerate(lines) if line.startswith("[R2"))
    assert not lines[label + 1].startswith("Sumă implicată")


@pytest.mark.skipif(pdftotext is None, reason="pdftotext indisponibil")
def test_signature_block_is_present(draft):
    with tempfile.TemporaryDirectory() as tmp:
        pdf = render_letter_pdf(draft, Path(tmp) / "letter.pdf")
        text = extract_text(pdf)
    assert "Data:" in text
    assert "Proprietar, apartamentul nr." in text
    assert "Nume și semnătură:" in text


@pytest.mark.skipif(pdftotext is None, reason="pdftotext indisponibil")
def test_rendering_does_not_alter_content(draft):
    """Randarea nu are voie să schimbe o formulare sau o sumă."""
    with tempfile.TemporaryDirectory() as tmp:
        pdf = render_letter_pdf(draft, Path(tmp) / "letter.pdf")
        text = " ".join(extract_text(pdf).split())
    assert draft.opening in text
    assert draft.coverage_paragraph in text
    assert draft.deadline_paragraph in text
    for _, paragraph in draft.paragraphs:
        assert paragraph in text

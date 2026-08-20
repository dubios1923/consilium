"""Drafter: din constatări, cererea formală de documente.

Modelul scrie proza. Nu calculează nimic și nu poate introduce o constatare care
nu există: fiecare paragraf trebuie să trimită la un `rule_id` din lista primită,
iar fiecare sumă din scrisoare trebuie să provină dintr-un câmp al unei
constatări deja calculate. Cererea se verifică programatic înainte de a fi
redactată; o scrisoare care nu trece verificarea nu se emite.

Motivul e practic, nu doctrinar: scrisoarea ajunge la un administrator care are
tot interesul să caute o cifră greșită în ea. O singură sumă inventată de model
discreditează întregul audit.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from google import genai
from google.genai import types
from pydantic import BaseModel

from consilium.config import Config
from consilium.reconciler import AuditResult, Finding
from consilium.schema import PaymentList

MODEL = "gemini-3.5-flash"
MAX_DRAFT_ATTEMPTS = 3

# Sume în format românesc: 1.234,56 sau 96,00. Numerele fără zecimale (număr de
# apartamente, articole de lege, ani) nu sunt sume și nu intră în verificare.
MONEY_PATTERN = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}")

# Cheile de repartizare sunt identificatori interni. In scrisoare trebuie sa
# apara in romana, altfel proprietarul trimite administratorului un enum.
KEY_LABELS = {
    "cota_indiviza": "cotă indiviză",
    "persoane": "număr de persoane",
    "consum": "consum contorizat",
    "egal": "părți egale",
}


def humanize(value: object) -> object:
    """Inlocuieste identificatorii de cheie cu eticheta lor romaneasca."""
    if not isinstance(value, str):
        return value
    for key, label in KEY_LABELS.items():
        value = value.replace(key, label)
    return value


class DrafterError(RuntimeError):
    """Scrisoarea generată nu a trecut verificarea."""


LetterMode = Literal[
    "contestatie", "contestatie_termen_necunoscut", "cerere_copii"
]


def letter_mode(result: AuditResult) -> LetterMode:
    """Ce document producem, decis de starea ferestrei de contestare.

    O listă de plată cu constatări nu se atacă printr-o simplă cerere de copii:
    art. 28 alin. (3) dă dreptul de a contesta modul de calcul al cotei, cu
    termen de răspuns și cale de escaladare. Dreptul se stinge însă la 10 zile de
    la afișare, iar după aceea rămâne doar dreptul de copii de la alin. (1), care
    nu are termen. R7 stabilește în care dintre situații ne aflăm.
    """
    types = {finding.finding_type for finding in result.findings}
    if "contestation_window_expired" in types:
        return "cerere_copii"
    if "contestation_window_open" in types:
        return "contestatie"
    return "contestatie_termen_necunoscut"


# --------------------------------------------------------------------------
# Schema răspunsului
# --------------------------------------------------------------------------


class _WireParagraph(BaseModel):
    finding_index: int
    rule_id: str
    text: str


class _WireLetter(BaseModel):
    subject: str
    opening: str
    finding_paragraphs: list[_WireParagraph]
    coverage_paragraph: str
    requested_documents: list[str]
    deadline_paragraph: str
    closing: str


@dataclass
class LetterDraft:
    """Scrisoarea verificată, gata de redactat."""

    subject: str
    opening: str
    paragraphs: list[tuple[Finding, str]]
    coverage_paragraph: str
    requested_documents: list[str]
    deadline_paragraph: str
    closing: str
    association_ref: str = ""
    period: str = ""
    document_id: str = ""
    audit_id: str = ""
    mode: LetterMode = "contestatie_termen_necunoscut"
    title: str = "CERERE DE COMUNICARE A DOCUMENTELOR"

    def all_text(self) -> str:
        return "\n".join(
            [
                self.subject,
                self.opening,
                *(text for _, text in self.paragraphs),
                self.coverage_paragraph,
                *self.requested_documents,
                self.deadline_paragraph,
                self.closing,
            ]
        )


@dataclass
class Verification:
    """Rezultatul verificării scrisorii față de constatări."""

    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


# --------------------------------------------------------------------------
# Verificarea
# --------------------------------------------------------------------------


def parse_money(token: str) -> float:
    return round(float(token.replace(".", "").replace(",", ".")), 2)


# Reconciler-ul formatează sumele din explicații cu punct zecimal ("470.67 lei").
# Sunt tot valori calculate de el, deci scrisoarea are dreptul să le citeze.
PLAIN_MONEY_PATTERN = re.compile(r"\d+\.\d{2}")


def amounts_in(text: str) -> set[float]:
    """Sumele care apar într-un text produs de reconciler."""
    values = {parse_money(token) for token in MONEY_PATTERN.findall(text)}
    values |= {round(float(token), 2) for token in PLAIN_MONEY_PATTERN.findall(text)}
    return values


def allowed_amounts(result: AuditResult, payment: PaymentList) -> set[float]:
    """Toate sumele pe care scrisoarea are dreptul să le citeze.

    Exclusiv valori deja calculate de reconciler sau tipărite în document. Nimic
    derivat aici, ca să nu deschidem pe ușa din spate exact aritmetica pe care o
    interzicem modelului.
    """
    amounts: set[float] = {0.0}
    for finding in result.findings:
        for value in (
            finding.expected_value,
            finding.found_value,
            finding.amount_involved,
        ):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                amounts.add(round(float(value), 2))
        # Explicația constatării e generată de reconciler, nu de model: sumele
        # din ea au aceeași proveniență ca și câmpurile numerice.
        amounts |= amounts_in(finding.message)
    amounts.add(result.total_amount_involved)
    amounts.add(round(payment.declared_totals.total_general, 2))
    for line in payment.expense_lines:
        amounts.add(round(line.amount, 2))
    for _, reason in result.coverage.expense_lines_unverified:
        amounts |= amounts_in(reason)
    return amounts


def verify_letter(
    draft: _WireLetter, result: AuditResult, payment: PaymentList
) -> Verification:
    """Fiecare paragraf trimite la o constatare reală; fiecare sumă provine din ea."""
    violations: list[str] = []
    findings = result.findings

    referenced: list[int] = []
    for position, paragraph in enumerate(draft.finding_paragraphs):
        if not 0 <= paragraph.finding_index < len(findings):
            violations.append(
                f"paragraful {position} trimite la constatarea inexistentă "
                f"{paragraph.finding_index}"
            )
            continue
        expected_rule = findings[paragraph.finding_index].rule_id
        if paragraph.rule_id != expected_rule:
            violations.append(
                f"paragraful {position} declară {paragraph.rule_id}, dar "
                f"constatarea {paragraph.finding_index} este {expected_rule}"
            )
        referenced.append(paragraph.finding_index)

    duplicates = {index for index in referenced if referenced.count(index) > 1}
    if duplicates:
        violations.append(f"constatări citate de mai multe ori: {sorted(duplicates)}")

    missing = sorted(set(range(len(findings))) - set(referenced))
    if missing:
        violations.append(f"constatări omise din scrisoare: {missing}")

    allowed = allowed_amounts(result, payment)
    text = "\n".join(
        [
            draft.subject,
            draft.opening,
            *(paragraph.text for paragraph in draft.finding_paragraphs),
            draft.coverage_paragraph,
            *draft.requested_documents,
            draft.deadline_paragraph,
            draft.closing,
        ]
    )
    for token in MONEY_PATTERN.findall(text):
        value = parse_money(token)
        if value not in allowed:
            violations.append(
                f"suma {token} nu provine din nicio constatare calculată"
            )

    return Verification(violations=violations)


# --------------------------------------------------------------------------
# Generarea
# --------------------------------------------------------------------------

SYSTEM_INSTRUCTION = """\
Redactezi cereri formale adresate asociațiilor de proprietari din România, în
numele unui proprietar, în temeiul art. 28 din Legea nr. 196/2018.

REGULI ABSOLUTE:
1. NU CALCULEZI NIMIC. Toate constatările și toate sumele îți sunt date deja
   calculate. Nu aduna, nu scădea, nu deduce procente și nu estima. Poți cita
   doar sume care apar literal în datele primite.
2. NU INVENTEZI CONSTATĂRI. Scrii exact câte un paragraf pentru fiecare
   constatare primită, în ordinea dată, și trimiți la ea prin `finding_index` și
   `rule_id`. Nu adaugi observații proprii despre document.
3. NU CONFUNDA TEMEIURILE. Art. 28 din Legea nr. 196/2018 conține alineate
   distincte, cu regimuri diferite. Invoci exact alineatul care ți se indică, cu
   termenul care ți se indică. Nu inventa un „termen legal de 10 zile" generic și
   nu atașa un termen unui drept care nu are.
4. NU ACUZI. Formulezi neutru și verificabil: „lista de plată prezintă o
   diferență de X lei între...", nu „administratorul a deturnat...". Cererea
   solicită documente, nu stabilește vinovății.
5. Ton formal, la persoana I singular, în limba română corectă, cu diacritice.
6. Sumele se scriu în format românesc, cu punct la mii și virgulă la zecimale,
   urmate de „lei": 1.234,56 lei.

Structura cerută:
- `subject`: obiectul cererii, o singură propoziție.
- `opening`: 2-3 propoziții care identifică lista de plată vizată și temeiul
  legal al cererii.
- `finding_paragraphs`: câte unul per constatare. Fiecare descrie neutru ce
  arată constatarea și ce document ar lămuri-o.
- `coverage_paragraph`: ce nu a putut fi verificat și de ce. Acesta este un
  paragraf esențial: spune ce lipsește din documentul publicat.
- `requested_documents`: lista documentelor solicitate, fiecare o linie de
  sine stătătoare, formulată ca cerere.
- `deadline_paragraph`: termenul de răspuns și consecința lipsei lui.
- `closing`: formula de încheiere, fără nume propriu (se completează manual).
"""

DRAFT_PROMPT = """\
Redactează cererea pentru următorul caz.

ASOCIAȚIA: {association}
LISTA DE PLATĂ: {document_id}, perioada {period}

CONSTATĂRI DEJA CALCULATE (le citezi, nu le recalculezi):
{findings}

RAPORT DE ACOPERIRE (ce nu s-a putut verifica):
{coverage}

DOCUMENTE PE CARE AUDITUL LE CERE DEJA:
{documents}

TEMEI ȘI TERMEN — folosește EXACT ce scrie mai jos. Nu invoca alt articol, alt
alineat, alt act normativ și niciun alt termen:
{legal_block}

Scrie câte un paragraf pentru fiecare dintre cele {count} constatări, în
ordinea de mai sus, cu `finding_index` egal cu indicele constatării.
{corrections}\
"""


def _findings_block(result: AuditResult) -> str:
    lines = []
    for index, finding in enumerate(result.findings):
        lines.append(
            json.dumps(
                {
                    "index": index,
                    "rule_id": finding.rule_id,
                    "tip": finding.finding_type,
                    "apartament": finding.apartment_no,
                    "categorie": finding.category,
                    "valoare_asteptata": humanize(finding.expected_value),
                    "valoare_gasita": humanize(finding.found_value),
                    "suma_implicata": finding.amount_involved,
                    "temei_legal": finding.legal_reference,
                    "explicatie": humanize(finding.message),
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines) if lines else "(nicio constatare)"


def _coverage_block(result: AuditResult) -> str:
    coverage = result.coverage
    lines = [f"- {coverage.summary()}"]
    for rule in coverage.rules:
        suffix = f" – {rule.reason}" if rule.reason else ""
        lines.append(
            f"- {rule.rule_id}: {rule.status} ({rule.checked}/{rule.total}){suffix}"
        )
    for category, reason in coverage.expense_lines_unverified:
        lines.append(f"- neverificat: {category} — {reason}")
    for number, reason in coverage.apartments_excluded:
        lines.append(f"- apartament exclus: {number} — {reason}")
    return "\n".join(lines)


MODE_GUIDANCE = {
    "contestatie": (
        "Documentul pe care îl redactezi este o CONTESTAȚIE privind modul de "
        "calcul al cotei de contribuție, nu o simplă cerere de documente.\n"
        "- temei: {art_28_3}\n"
        "  ({right_28_3})\n"
        "- fereastra de contestare este DESCHISĂ; termenul exact și numărul de "
        "zile rămase îți sunt date în constatarea R7 — citează-le de acolo, nu "
        "le recalcula.\n"
        "- președintele asociației are obligația de a răspunde în scris în "
        "{response_days} zile de la primirea contestației.\n"
        "- dacă solicitările nu sunt soluționate în {unresolved_days} zile de la "
        "depunere, te poți adresa compartimentului specializat al autorității "
        "administrației publice locale, în temeiul {art_28_4}. Menționează asta "
        "ca etapă următoare, nu ca amenințare.\n"
        "- solicitarea de copii după documente se întemeiază pe {art_28_1}, "
        "care nu are termen atașat."
    ),
    "contestatie_termen_necunoscut": (
        "Documentul este o CONTESTAȚIE privind modul de calcul al cotei de "
        "contribuție, întemeiată pe {art_28_3} ({right_28_3}).\n"
        "- ATENȚIE: data afișării listei la avizier nu a putut fi stabilită din "
        "document, deci nu se poate confirma dacă termenul de contestare mai "
        "este deschis. Spune asta explicit în scrisoare, într-o propoziție, și "
        "cere confirmarea datei afișării.\n"
        "- președintele răspunde în scris în {response_days} zile de la primire.\n"
        "- escaladare la autoritatea locală după {unresolved_days} zile de "
        "nesoluționare, în temeiul {art_28_4}.\n"
        "- copiile după documente se cer în temeiul {art_28_1}, fără termen."
    ),
    "cerere_copii": (
        "Documentul este o CERERE DE COMUNICARE A DOCUMENTELOR, întemeiată pe "
        "{art_28_1} ({right_28_1}), drept care NU are termen atașat.\n"
        "- ATENȚIE: termenul de contestare a modului de calcul al cotei a "
        "EXPIRAT pentru această listă; detaliile îți sunt date în constatarea "
        "R7. NU formula o contestație și NU invoca {art_28_3} ca temei al unei "
        "contestații pentru această listă.\n"
        "- menționează că observațiile rămân valabile pentru contestarea listei "
        "următoare, în termen.\n"
        "- paragraful de termen: dreptul de la {art_28_1} NU are termen legal "
        "atașat, deci NU scrie \u201ein termenul legal\u201d și nu inventa unul. "
        "Singurul "
        "termen invocabil aici este {art_28_4}: dacă solicitarea nu este "
        "soluționată în {unresolved_days} zile de la depunere, te poți adresa "
        "compartimentului specializat al autorității administrației publice "
        "locale."
    ),
}


def legal_block(mode: LetterMode, config: Config) -> str:
    """Instrucțiunea juridică exactă pentru modul în care ne aflăm."""
    return MODE_GUIDANCE[mode].format(
        art_28_1=config.require("legal.art_28_1.reference"),
        art_28_3=config.require("legal.art_28_3.reference"),
        art_28_4=config.require("legal.art_28_4.reference"),
        right_28_1=config.require("legal.art_28_1.right").strip(),
        right_28_3=config.require("legal.art_28_3.right").strip(),
        response_days=config.require_int("legal.art_28_3.response_days"),
        unresolved_days=config.require_int("legal.art_28_4.unresolved_days"),
    )


def draft_letter(
    payment: PaymentList,
    result: AuditResult,
    client: genai.Client,
    config: Config,
    audit_id: str = "",
) -> LetterDraft:
    """Generează și verifică scrisoarea. Ridică DrafterError dacă nu trece."""
    mode = letter_mode(result)
    block = legal_block(mode, config)
    corrections = ""
    last: Verification | None = None

    for attempt in range(1, MAX_DRAFT_ATTEMPTS + 1):
        prompt = DRAFT_PROMPT.format(
            association=payment.association_ref,
            document_id=payment.document_id,
            period=payment.period,
            findings=_findings_block(result),
            coverage=_coverage_block(result),
            documents="\n".join(
                f"- {document}" for document in result.coverage.documents_to_request
            ),
            count=len(result.findings),
            legal_block=block,
            corrections=corrections,
        )
        response = client.models.generate_content(
            model=MODEL,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(
                temperature=0.2,
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=_WireLetter,
            ),
        )
        wire = response.parsed
        if wire is None:
            wire = _WireLetter.model_validate_json(response.text or "")

        last = verify_letter(wire, result, payment)
        if last.ok:
            return LetterDraft(
                subject=wire.subject,
                opening=wire.opening,
                paragraphs=[
                    (result.findings[paragraph.finding_index], paragraph.text)
                    for paragraph in wire.finding_paragraphs
                ],
                coverage_paragraph=wire.coverage_paragraph,
                requested_documents=list(wire.requested_documents),
                deadline_paragraph=wire.deadline_paragraph,
                closing=wire.closing,
                association_ref=payment.association_ref,
                period=payment.period,
                document_id=payment.document_id,
                audit_id=audit_id,
                mode=mode,
                title=config.require(f"drafter.mode_labels.{mode}"),
            )
        if attempt < MAX_DRAFT_ATTEMPTS:
            corrections = (
                "\nÎNCERCAREA ANTERIOARĂ A FOST RESPINSĂ. Corectează exact "
                "următoarele și nu schimba nimic altceva:\n"
                + "\n".join(f"- {violation}" for violation in last.violations)
                + "\n"
            )

    raise DrafterError(
        "scrisoarea nu a trecut verificarea după "
        f"{MAX_DRAFT_ATTEMPTS} încercări: "
        + "; ".join(last.violations if last else ["motiv necunoscut"])
    )


# --------------------------------------------------------------------------
# Artefacte
# --------------------------------------------------------------------------

# Fontul scrisorii trebuie să aibă glifele românești complete, inclusiv ș și ț
# cu virgulă dedesubt (U+0219/U+021B), care NU sunt în Latin-1. Familiile sunt
# căutate în ordinea preferinței, recursiv: căile diferă între distribuții
# (Fedora pune Liberation în /usr/share/fonts/liberation-sans-fonts/, Debian în
# /usr/share/fonts/truetype/liberation/), iar o listă de căi fixe a produs deja
# un PDF de producție randat integral cu Helvetica și diacriticele înlocuite de
# pătrate. De aceea aici nu există fallback: fără font potrivit, se ridică
# excepție.
_FONT_FAMILIES: list[tuple[str, str]] = [
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf"),
    ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf"),
    ("FreeSans.ttf", "FreeSansBold.ttf"),
]

_FONT_SEARCH_DIRS = [
    Path(__file__).resolve().parent.parent / "assets" / "fonts",
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path.home() / ".local" / "share" / "fonts",
    Path.home() / ".fonts",
]

# Fără aceste caractere documentul nu e lizibil în română.
REQUIRED_GLYPHS = "ăâîșțĂÂÎȘȚ„”"

FONT_REGULAR = "ConsiliumSans"
FONT_BOLD = "ConsiliumSans-Bold"


class FontError(RuntimeError):
    """Niciun font cu acoperire românească completă nu a fost găsit."""


def _find_font_file(filename: str) -> Path | None:
    for directory in _FONT_SEARCH_DIRS:
        if not directory.is_dir():
            continue
        for candidate in directory.rglob(filename):
            if candidate.is_file():
                return candidate
    return None


def _missing_glyphs(font_name: str) -> str:
    """Caracterele românești pe care fontul înregistrat nu le poate reda."""
    from reportlab.pdfbase import pdfmetrics

    mapping = pdfmetrics.getFont(font_name).face.charToGlyph
    return "".join(ch for ch in REQUIRED_GLYPHS if ord(ch) not in mapping)


FINDINGS_CSV_COLUMNS = [
    "rule_id",
    "severity",
    "finding_type",
    "apartment_no",
    "category",
    "expected_value",
    "found_value",
    "amount_involved",
    "legal_reference",
    "message",
]


def _register_fonts() -> tuple[str, str]:
    """Înregistrează un TTF cu acoperire românească. Ridică FontError dacă nu există.

    Nu cade înapoi pe Helvetica: Helvetica nu are ă, â, î, ș, ț, iar rezultatul
    e un document care arată corect în cod și e ilizibil pe hârtie.
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if FONT_REGULAR in pdfmetrics.getRegisteredFontNames():
        return FONT_REGULAR, FONT_BOLD

    tried: list[str] = []
    for regular_name, bold_name in _FONT_FAMILIES:
        regular = _find_font_file(regular_name)
        bold = _find_font_file(bold_name)
        if regular is None or bold is None:
            tried.append(regular_name)
            continue
        pdfmetrics.registerFont(TTFont(FONT_REGULAR, str(regular)))
        pdfmetrics.registerFont(TTFont(FONT_BOLD, str(bold)))
        missing = _missing_glyphs(FONT_REGULAR)
        if missing:
            raise FontError(
                f"{regular} nu conține glifele românești: {missing}. "
                "Instalează fonts-dejavu-core sau fonts-liberation."
            )
        return FONT_REGULAR, FONT_BOLD

    raise FontError(
        "niciun font cu acoperire românească nu a fost găsit (căutat: "
        + ", ".join(tried)
        + " în "
        + ", ".join(str(d) for d in _FONT_SEARCH_DIRS)
        + "). Instalează fonts-dejavu-core sau fonts-liberation."
    )


def format_money(value: float) -> str:
    text = f"{value:,.2f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def render_letter_pdf(draft: LetterDraft, target: Path) -> Path:
    """Redactează scrisoarea ca PDF A4, format de corespondență oficială.

    Doar randare: textul, sumele și structura vin din `draft` neatinse.
    """
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        KeepTogether,
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    regular, bold = _register_fonts()

    body = ParagraphStyle(
        "body", fontName=regular, fontSize=10.5, leading=15.5,
        alignment=TA_JUSTIFY, spaceAfter=8,
    )
    # Titlurile de secțiune rămân lipite de ce urmează: un titlu singur în
    # subsolul paginii arată ca o scrisoare ruptă.
    heading = ParagraphStyle(
        "heading", fontName=bold, fontSize=11.5, leading=15,
        spaceBefore=20, spaceAfter=9, keepWithNext=1,
    )
    title = ParagraphStyle(
        "title", fontName=bold, fontSize=14, leading=19,
        spaceBefore=4, spaceAfter=12,
    )
    subject = ParagraphStyle(
        "subject", fontName=regular, fontSize=10.5, leading=15,
        alignment=TA_JUSTIFY, spaceAfter=14,
    )
    meta = ParagraphStyle(
        "meta", fontName=regular, fontSize=9, leading=13.5, spaceAfter=3,
    )
    finding_label = ParagraphStyle(
        "finding_label", fontName=bold, fontSize=10, leading=14,
        spaceBefore=4, spaceAfter=1, keepWithNext=1,
    )
    finding_amount = ParagraphStyle(
        "finding_amount", fontName=regular, fontSize=9.5, leading=13,
        spaceAfter=5, keepWithNext=1,
    )
    listed = ParagraphStyle(
        "listed", fontName=regular, fontSize=10.5, leading=15.5,
        alignment=TA_JUSTIFY, spaceAfter=9,
    )
    signature = ParagraphStyle(
        "signature", fontName=regular, fontSize=9.5, leading=22,
    )

    document = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=25 * mm,
        rightMargin=25 * mm,
        topMargin=22 * mm,
        bottomMargin=20 * mm,
        title=draft.subject,
        invariant=1,
    )

    story: list[Any] = [
        Paragraph(f"Către: {draft.association_ref}", meta),
        Paragraph(
            f"Referință: lista de plată {draft.document_id}, perioada "
            f"{draft.period}",
            meta,
        ),
    ]
    if draft.audit_id:
        story.append(Paragraph(f"Dosar de verificare: {draft.audit_id}", meta))
    story += [
        Spacer(1, 16),
        Paragraph(draft.title, title),
        Paragraph(draft.subject, subject),
        Paragraph(draft.opening, body),
    ]

    if draft.paragraphs:
        story.append(Paragraph("Constatări", heading))
        for finding, text in draft.paragraphs:
            label = finding.rule_id
            if finding.apartment_no:
                label += f", apartamentul {finding.apartment_no}"
            if finding.category:
                label += f", „{finding.category}”"

            # Fiecare constatare e un bloc: eticheta regulii pe un rând, suma pe
            # rândul următor, apoi explicația. Blocul nu se rupe între pagini.
            block: list[Any] = [Paragraph(f"[{label}]", finding_label)]
            if finding.amount_involved is not None:
                block.append(
                    Paragraph(
                        f"Sumă implicată: "
                        f"{format_money(finding.amount_involved)} lei",
                        finding_amount,
                    )
                )
            block.append(Paragraph(text, body))
            story.append(KeepTogether(block))

    story.append(Paragraph("Verificări care nu au putut fi efectuate", heading))
    story.append(Paragraph(draft.coverage_paragraph, body))

    story.append(Paragraph("Documente solicitate", heading))
    # Spațiu explicit între titlu și primul număr: fără el, „1” urcă până sub
    # titlu și cele două se citesc ca „Documente solicitate1”.
    story.append(Spacer(1, 3))
    story.append(
        ListFlowable(
            [
                ListItem(Paragraph(item, listed), leftIndent=22, value=index)
                for index, item in enumerate(draft.requested_documents, start=1)
            ],
            bulletType="1",
            bulletFontName=bold,
            bulletFontSize=10.5,
            bulletDedent=22,
            leftIndent=22,
            start=1,
        )
    )

    story.append(Paragraph("Termen de răspuns", heading))
    story.append(Paragraph(draft.deadline_paragraph, body))

    story.append(Spacer(1, 22))
    story.append(Paragraph(draft.closing, body))
    story.append(Spacer(1, 34))
    story.append(
        KeepTogether(
            [
                Paragraph("Data: __________________", signature),
                Paragraph("Proprietar, apartamentul nr. __________", signature),
                Paragraph(
                    "Nume și semnătură: ______________________________",
                    signature,
                ),
            ]
        )
    )

    document.build(story)
    return target


def findings_csv(result: AuditResult) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FINDINGS_CSV_COLUMNS)
    writer.writeheader()
    for finding in result.findings:
        writer.writerow(
            {column: getattr(finding, column) for column in FINDINGS_CSV_COLUMNS}
        )
    return buffer.getvalue()


def audit_report_json(
    payment: PaymentList, result: AuditResult, audit_id: str
) -> str:
    coverage = result.coverage
    payload = {
        "audit_id": audit_id,
        "document_id": payment.document_id,
        "association_ref": payment.association_ref,
        "period": payment.period,
        "declared_totals": payment.declared_totals.model_dump(),
        "findings": [
            {column: getattr(finding, column) for column in FINDINGS_CSV_COLUMNS}
            for finding in result.findings
        ],
        "total_amount_involved": result.total_amount_involved,
        "coverage_report": {
            "summary": coverage.summary(),
            "rules": [
                {
                    "rule_id": rule.rule_id,
                    "status": rule.status,
                    "checked": rule.checked,
                    "total": rule.total,
                    "reason": rule.reason,
                }
                for rule in coverage.rules
            ],
            "expense_lines_total": coverage.expense_lines_total,
            "expense_lines_verified": coverage.expense_lines_verified,
            "expense_lines_unverified": [
                {"category": category, "reason": reason}
                for category, reason in coverage.expense_lines_unverified
            ],
            "apartments_total": coverage.apartments_total,
            "apartments_excluded": [
                {"apartment_no": number, "reason": reason}
                for number, reason in coverage.apartments_excluded
            ],
            "documents_to_request": coverage.documents_to_request,
        },
        "extraction_confidence": payment.extraction_confidence.model_dump(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def write_artifacts(
    destination: str,
    audit_id: str,
    payment: PaymentList,
    result: AuditResult,
    draft: LetterDraft,
) -> list[str]:
    """Scrie cele trei artefacte local sau în GCS. Întoarce URI-urile."""
    letter_name = f"cerere_documente_{audit_id}.pdf"
    files = {
        "findings.csv": findings_csv(result).encode("utf-8"),
        "audit_report.json": audit_report_json(payment, result, audit_id).encode(
            "utf-8"
        ),
    }

    if destination.startswith("gs://"):
        import tempfile

        from google.cloud import storage

        bucket_name, _, prefix = destination[5:].partition("/")
        prefix = prefix.rstrip("/")
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        uris: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            local_pdf = render_letter_pdf(draft, Path(tmp) / letter_name)
            files[letter_name] = local_pdf.read_bytes()
            for name, payload in files.items():
                blob_path = f"{prefix}/{name}" if prefix else name
                blob = bucket.blob(blob_path)
                blob.upload_from_string(
                    payload,
                    content_type=(
                        "application/pdf"
                        if name.endswith(".pdf")
                        else "text/csv"
                        if name.endswith(".csv")
                        else "application/json"
                    ),
                )
                uris.append(f"gs://{bucket_name}/{blob_path}")
        return sorted(uris)

    out_dir = Path(destination)
    out_dir.mkdir(parents=True, exist_ok=True)
    render_letter_pdf(draft, out_dir / letter_name)
    for name, payload in files.items():
        (out_dir / name).write_bytes(payload)
    return sorted(str(out_dir / name) for name in [letter_name, *files])

"""R0 – verificarea coerenței transcrierii, înainte de orice audit.

R0 nu este o regulă de audit. Nu spune „asociația a greșit”, spune „nu am
încredere în ce am citit”. Distincția e esențială: o celulă citită greșit dintr-un
scan produce exact aceeași aritmetică ruptă ca o repartizare frauduloasă, iar un
auditor care nu le separă raportează fals pozitive cu aer de certitudine.

Două verificări, ambele deterministe și fără rețea:
  R0.row    – pe fiecare apartament: Σ charges + arrears + penalties == total_due
  R0.column – pe fiecare categorie: Σ pe apartamente == suma din expense_lines

Când un rând și o coloană pică cu aceeași diferență, intersecția lor localizează
o singură celulă: acolo se face recitirea țintită.

LIMITARE CUNOSCUTĂ. R0 este o verificare aritmetică, deci acoperă doar câmpurile
care participă la o sumă. Câmpurile textuale nu au cum să fie prinse: pe
sample_clean_scanned, referința de factură „FT-2025-11-000877" a fost citită
„FF-2025-11-000877" (confuzie T/F la 150 dpi) și nicio verificare de acest tip
nu o poate semnala. Consecința practică este limitată (referința se folosește la
cererea documentului justificativ, nu la calcule), dar proprietarul trebuie
avertizat că un cod de factură transcris dintr-un scan poate fi greșit cu o
literă. Acoperirea ar cere o a doua sursă (registrul de facturi al asociației),
nu o regulă suplimentară.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from consilium.config import Config
from consilium.schema import PaymentList

RULE_ROW = "R0.row"
RULE_COLUMN = "R0.column"

Scope = Literal["row", "column"]


def _round(value: float) -> float:
    return round(value + 0.0, 2)


@dataclass(frozen=True)
class IntegrityIssue:
    """O incoerență aritmetică internă a transcrierii."""

    rule_id: str
    scope: Scope
    expected: float
    found: float
    delta: float
    message: str
    apartment_no: str | None = None
    category: str | None = None


@dataclass(frozen=True)
class SuspectCell:
    """O celulă localizată la intersecția unui rând și a unei coloane suspecte."""

    apartment_no: str
    category: str
    delta: float


@dataclass(frozen=True)
class IntegrityReport:
    """Rezultatul R0 pe un document extras."""

    row_issues: list[IntegrityIssue] = field(default_factory=list)
    column_issues: list[IntegrityIssue] = field(default_factory=list)
    suspect_cells: list[SuspectCell] = field(default_factory=list)
    rows_checked: int = 0
    columns_checked: int = 0

    @property
    def issues(self) -> list[IntegrityIssue]:
        return [*self.row_issues, *self.column_issues]

    @property
    def is_clean(self) -> bool:
        return not self.row_issues and not self.column_issues

    @property
    def suspect_apartments(self) -> set[str]:
        return {
            issue.apartment_no
            for issue in self.row_issues
            if issue.apartment_no is not None
        }

    @property
    def suspect_categories(self) -> set[str]:
        return {
            issue.category
            for issue in self.column_issues
            if issue.category is not None
        }


def check_rows(payment: PaymentList, tolerance: float) -> list[IntegrityIssue]:
    """Σ charges + arrears + penalties trebuie să dea total_due, pe fiecare rând."""
    issues: list[IntegrityIssue] = []
    for line in payment.apartment_lines:
        computed = _round(
            sum(line.charges.values()) + line.arrears + line.penalties
        )
        delta = _round(computed - line.total_due)
        if abs(delta) > tolerance:
            issues.append(
                IntegrityIssue(
                    rule_id=RULE_ROW,
                    scope="row",
                    apartment_no=line.apartment_no,
                    expected=line.total_due,
                    found=computed,
                    delta=delta,
                    message=(
                        f"Apartamentul {line.apartment_no}: suma defalcării "
                        f"({computed:.2f}) nu dă totalul de plată tipărit "
                        f"({line.total_due:.2f}); diferență {delta:+.2f} lei."
                    ),
                )
            )
    return issues


def check_columns(payment: PaymentList, tolerance: float) -> list[IntegrityIssue]:
    """Σ pe apartamente a fiecărei categorii trebuie să dea suma declarată."""
    issues: list[IntegrityIssue] = []
    for expense in payment.expense_lines:
        present = [
            line.charges[expense.category]
            for line in payment.apartment_lines
            if expense.category in line.charges
        ]
        missing = len(payment.apartment_lines) - len(present)
        computed = _round(sum(present))
        delta = _round(computed - expense.amount)
        if abs(delta) <= tolerance and missing == 0:
            continue
        suffix = (
            f" ({missing} apartamente nu au deloc această coloană)"
            if missing
            else ""
        )
        issues.append(
            IntegrityIssue(
                rule_id=RULE_COLUMN,
                scope="column",
                category=expense.category,
                expected=expense.amount,
                found=computed,
                delta=delta,
                message=(
                    f"Coloana „{expense.category}”: suma pe apartamente "
                    f"({computed:.2f}) nu dă suma declarată în tabelul de "
                    f"cheltuieli ({expense.amount:.2f}); diferență "
                    f"{delta:+.2f} lei{suffix}."
                ),
            )
        )
    return issues


def localize(
    row_issues: list[IntegrityIssue],
    column_issues: list[IntegrityIssue],
    tolerance: float,
) -> list[SuspectCell]:
    """Intersectează rândurile și coloanele suspecte cu aceeași diferență.

    Dacă rândul 7 lipsește 7,60 lei și coloana „Apă caldă” lipsește tot 7,60 lei,
    celula (7, Apă caldă) este singura explicație simplă. Recitirea se poate
    concentra pe ea, în loc să reia tot rândul.
    """
    cells: list[SuspectCell] = []
    for row in row_issues:
        matches = [
            column
            for column in column_issues
            if abs(column.delta - row.delta) <= tolerance
        ]
        if len(matches) != 1 or row.apartment_no is None:
            continue
        category = matches[0].category
        if category is None:
            continue
        cells.append(
            SuspectCell(
                apartment_no=row.apartment_no, category=category, delta=row.delta
            )
        )
    return cells


def check_integrity(payment: PaymentList, config: Config) -> IntegrityReport:
    """Rulează R0 complet. Determinist, fără rețea."""
    row_tolerance = config.require_float("integrity.row_total_tolerance")
    column_tolerance = config.require_float("integrity.column_total_tolerance")

    row_issues = check_rows(payment, row_tolerance)
    column_issues = check_columns(payment, column_tolerance)
    return IntegrityReport(
        row_issues=row_issues,
        column_issues=column_issues,
        suspect_cells=localize(row_issues, column_issues, row_tolerance),
        rows_checked=len(payment.apartment_lines),
        columns_checked=len(payment.expense_lines),
    )


# --------------------------------------------------------------------------
# Rezultatul recitirii tintite
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """Ce s-a intamplat cu fiecare esec R0 dupa recitirea zonei suspecte.

    `confirmed_inconsistencies` sunt esecuri reconfirmate de o a doua citire:
    documentul chiar nu se inchide aritmetic, deci sunt constatari de audit.
    Restul raman incertitudini de transcriere: randurile lor nu intra in audit,
    ci in coverage report.
    """

    report: IntegrityReport
    confirmed_inconsistencies: list[IntegrityIssue] = field(default_factory=list)
    unauditable_apartments: set[str] = field(default_factory=set)
    unauditable_categories: set[str] = field(default_factory=set)
    low_confidence_fields: list[str] = field(default_factory=list)
    reread_calls: int = 0

    @property
    def is_fully_auditable(self) -> bool:
        return not self.unauditable_apartments and not self.unauditable_categories

    def is_suspect(self, apartment_no: str, field: str) -> bool:
        """Spune daca un camp anume al unui apartament e nesigur.

        Marcajele sunt cai precise (`apartment_lines[7].charges['Apă caldă']`),
        deci o regula care nu atinge acel camp nu trebuie sa piarda tot randul.
        O cale care marcheaza randul intreg (`apartment_lines[7]`) le acopera pe
        toate.
        """
        row_prefix = f"apartment_lines[{apartment_no}]"
        exact = f"{row_prefix}.{field}"
        for flagged in self.low_confidence_fields:
            if flagged == row_prefix or flagged == exact:
                return True
            if flagged.startswith((f"{exact}[", f"{exact}.")):
                return True
        return False

    def suspect_charge(self, apartment_no: str, category: str) -> bool:
        """Varianta pentru o celula de cheltuiala.

        Se potriveste doar pe celula exacta, pe intreg blocul `charges` sau pe
        randul intreg. Un marcaj pe o singura celula nu trebuie sa faca
        neverificabile si celelalte categorii ale aceluiasi apartament, fiindca asta ar
        transforma o eroare de citire intr-o gaura de acoperire de opt ori mai
        mare decat este.
        """
        row_prefix = f"apartment_lines[{apartment_no}]"
        matches = {
            row_prefix,
            f"{row_prefix}.charges",
            f"{row_prefix}.charges[{category!r}]",
        }
        return any(flagged in matches for flagged in self.low_confidence_fields)


def without_reread(report: IntegrityReport) -> Resolution:
    """Rezolutia conservatoare, cand recitirea nu e disponibila.

    Fara o a doua citire nu se poate distinge o eroare de transcriere de o
    inconsistenta reala a documentului, deci nimic nu se declara constatare de
    audit: tot ce a picat R0 devine neauditabil.
    """
    fields = [
        f"apartment_lines[{issue.apartment_no}].<rand>" for issue in report.row_issues
    ] + [f"expense_lines[{issue.category!r}].<coloana>" for issue in report.column_issues]
    return Resolution(
        report=report,
        unauditable_apartments=set(report.suspect_apartments),
        unauditable_categories=set(report.suspect_categories),
        low_confidence_fields=fields,
    )

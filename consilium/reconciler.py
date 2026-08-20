"""Reconciler determinist: PaymentList validat -> listă de constatări tipizate.

Acest modul nu importă niciun SDK de model și nu face niciun apel de rețea.
Regula nu e stilistică: o constatare de audit trebuie să fie reproductibilă și
explicabilă rând cu rând în fața unui administrator sau a unei instanțe. Dacă un
test ar avea nevoie de un model ca să treacă, testul e greșit.

Reguli:
  R1  suma expense_lines == declared_totals.total_general
  R2  suma cota_indiviza pe toate apartamentele == 100%
  R3  fiecare cheltuială repartizată conform distribution_key declarate
  R4  penalizări <= plafonul legal pe zi din restanță (Legea 196/2018)
  R5  cheltuiala lunii curente pe apartamente == total_general
  R6  numărul de apartamente găsite == declared_totals.apartment_count

R5 compară `total_due` din care se scad restanțele și penalizările. Acelea sunt
istoric, nu cheltuială repartizată în luna curentă; comparate direct cu totalul
general ar produce o constatare pe orice listă corectă în care cineva are datorii.

Ce nu s-a putut verifica nu dispare: intră în `CoverageReport`. Un audit care își
ascunde punctele oarbe e mai rău decât unul care nu rulează, pentru că
proprietarul crede că a fost verificat tot.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from consilium.config import Config
from consilium.integrity import IntegrityIssue, Resolution
from consilium.schema import ApartmentLine, ExpenseLine, PaymentList

Severity = Literal["high", "medium", "low", "info"]

RULE_IDS = ("R1", "R2", "R3", "R4", "R5", "R6", "R7")


def _round(value: float) -> float:
    return round(value + 0.0, 2)


def lei(value: float, signed: bool = False) -> str:
    """O suma in format romanesc: 1.234,56

    Constatarile ajung intr-o scrisoare romaneasca si pe o pagina romaneasca.
    Un `27689.95 lei` langa un `3.343,87 lei` in acelasi paragraf arata ca o
    scapare, pentru ca este. Formatarea sta aici, nu in drafter, fiindca drafter
    importa reconciler si invers ar fi ciclic.
    """
    text = f"{abs(value):,.2f}".replace(",", "\x00").replace(".", ",")
    text = text.replace("\x00", ".")
    if signed:
        return f"{'-' if value < 0 else '+'}{text}"
    return text


def percent(value: float, decimals: int = 2) -> str:
    """Un procent in format romanesc: 0,20 sau 99,20"""
    return f"{value:.{decimals}f}".replace(".", ",")


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in stripped.casefold() if ch.isalnum())


# --------------------------------------------------------------------------
# Tipuri de ieșire
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """O constatare de audit: ce s-a așteptat, ce s-a găsit, cât costă."""

    rule_id: str
    severity: Severity
    finding_type: str
    message: str
    expected_value: float | str | None = None
    found_value: float | str | None = None
    amount_involved: float | None = None
    apartment_no: str | None = None
    category: str | None = None
    legal_reference: str | None = None


@dataclass(frozen=True)
class RuleCoverage:
    """Cât din document a putut atinge o regulă."""

    rule_id: str
    status: Literal["verificat", "parțial", "neverificabil"]
    checked: int
    total: int
    reason: str | None = None


@dataclass(frozen=True)
class CoverageReport:
    """Ce s-a verificat, ce nu, și de ce.

    Este parte din rezultat, nu un jurnal de depanare: „6 din 8 poziții
    verificabile; 2 necesită date de consum absente din document" îi spune
    proprietarului exact ce documente să ceară în plus.
    """

    rules: list[RuleCoverage] = field(default_factory=list)
    expense_lines_total: int = 0
    expense_lines_verified: list[str] = field(default_factory=list)
    expense_lines_unverified: list[tuple[str, str]] = field(default_factory=list)
    apartments_total: int = 0
    apartments_excluded: list[tuple[str, str]] = field(default_factory=list)
    documents_to_request: list[str] = field(default_factory=list)

    def summary(self) -> str:
        verified = len(self.expense_lines_verified)
        return (
            f"{verified} din {self.expense_lines_total} poziții de cheltuială "
            f"verificabile"
        )


@dataclass(frozen=True)
class AuditResult:
    findings: list[Finding] = field(default_factory=list)
    coverage: CoverageReport = field(default_factory=CoverageReport)

    def by_rule(self, rule_id: str) -> list[Finding]:
        return [finding for finding in self.findings if finding.rule_id == rule_id]

    @property
    def total_amount_involved(self) -> float:
        return _round(
            sum(
                finding.amount_involved or 0.0
                for finding in self.findings
                if finding.severity != "info"
            )
        )


# --------------------------------------------------------------------------
# Ponderile de repartizare
# --------------------------------------------------------------------------


def _consumption_field(category: str, keywords: dict[str, list[str]]) -> str | None:
    """Găsește câmpul de consum care corespunde unei categorii."""
    normalized = _normalize(category)
    best: tuple[int, str] | None = None
    for field_name, patterns in keywords.items():
        for pattern in patterns:
            token = _normalize(pattern)
            if token and token in normalized and (best is None or len(token) > best[0]):
                best = (len(token), field_name)
    return best[1] if best else None


def distribution_weights(
    expense: ExpenseLine, apartments: list[ApartmentLine], config: Config
) -> tuple[list[float] | None, str | None]:
    """Ponderile teoretice pentru o poziție de cheltuială.

    Întoarce (ponderi, motiv_indisponibilitate). Ponderile lipsesc doar când
    documentul nu conține datele necesare, caz în care poziția merge în
    coverage report, nu într-o constatare.
    """
    if expense.distribution_key == "cota_indiviza":
        return [line.cota_indiviza for line in apartments], None
    if expense.distribution_key == "persoane":
        return [float(line.persons) for line in apartments], None
    if expense.distribution_key == "egal":
        return [1.0] * len(apartments), None

    keywords = config.require("reconciler.consumption_field_keywords")
    field_name = _consumption_field(expense.category, keywords)
    if field_name is None:
        return None, (
            "cheia declarată este „consum”, dar categoria nu se potrivește "
            "niciunui câmp de consum cunoscut"
        )
    values: list[float] = []
    for line in apartments:
        if line.consumption is None:
            return None, (
                "documentul nu publică consumurile individuale necesare "
                "verificării repartizării pe consum"
            )
        value = getattr(line.consumption, field_name)
        if value is None:
            return None, (
                f"consumul „{field_name}” lipsește pentru apartamentul "
                f"{line.apartment_no}"
            )
        values.append(float(value))
    if sum(values) <= 0:
        return None, "consumurile publicate însumează zero"
    return values, None


def expected_shares(amount: float, weights: list[float]) -> list[float]:
    """Cota teoretică a fiecărui apartament, normalizată la ponderile reale.

    Normalizarea se face la suma cotelor găsite în document, nu la 100%: dacă
    suma cotelor e greșită, asta e treaba R2, iar amestecarea celor două ar
    produce o constatare R3 pe fiecare apartament pentru o singură eroare.
    """
    total = sum(weights)
    return [amount * weight / total for weight in weights]


# --------------------------------------------------------------------------
# Regulile
# --------------------------------------------------------------------------


def rule_r1(payment: PaymentList, config: Config) -> list[Finding]:
    """Suma pozițiilor de cheltuială trebuie să dea totalul general declarat."""
    tolerance = config.require_float("reconciler.total_tolerance")
    declared = payment.declared_totals.total_general
    computed = _round(sum(line.amount for line in payment.expense_lines))
    delta = _round(computed - declared)
    if abs(delta) <= tolerance:
        return []
    return [
        Finding(
            rule_id="R1",
            severity="high",
            finding_type="declared_total_mismatch",
            expected_value=computed,
            found_value=declared,
            amount_involved=abs(delta),
            message=(
                f"Totalul general declarat ({lei(declared)} lei) nu corespunde "
                f"sumei celor {len(payment.expense_lines)} poziții de cheltuială "
                f"({lei(computed)} lei); diferență {lei(-delta, signed=True)} lei."
            ),
        )
    ]


def rule_r2(
    payment: PaymentList, config: Config, resolution: Resolution | None
) -> tuple[list[Finding], RuleCoverage]:
    """Suma cotelor indivize trebuie să acopere exact proprietatea comună."""
    target = config.require_float("reconciler.cota_sum_target")
    tolerance = config.require_float("reconciler.cota_sum_tolerance")
    total = len(payment.apartment_lines)

    suspect = [
        line.apartment_no
        for line in payment.apartment_lines
        if resolution and resolution.is_suspect(line.apartment_no, "cota_indiviza")
    ]
    if suspect:
        return [], RuleCoverage(
            "R2",
            "neverificabil",
            total - len(suspect),
            total,
            f"cotă indiviză incertă la apartamentele {', '.join(sorted(suspect))}",
        )

    computed = _round(sum(line.cota_indiviza for line in payment.apartment_lines))
    coverage = RuleCoverage("R2", "verificat", total, total)
    delta = _round(computed - target)
    if abs(delta) <= tolerance:
        return [], coverage
    direction = "lipsesc" if delta < 0 else "sunt în plus"
    return [
        Finding(
            rule_id="R2",
            severity="high",
            finding_type="cota_indiviza_sum_mismatch",
            expected_value=target,
            found_value=computed,
            amount_involved=None,
            legal_reference=config.require("reconciler.legal_references.R2"),
            message=(
                f"Suma cotelor indivize este {percent(computed)}%, nu {percent(target)}%: "
                f"{percent(abs(delta))} puncte procentuale {direction}. "
                f"Cheltuielile repartizate pe cotă se împart astfel pe o bază "
                f"greșită."
            ),
        )
    ], coverage


def rule_r3(
    payment: PaymentList, config: Config, resolution: Resolution | None
) -> tuple[list[Finding], RuleCoverage, list[str], list[tuple[str, str]]]:
    """Fiecare cheltuială trebuie repartizată conform cheii pe care o declară.

    Două verificări, nu una:
      - per apartament: nicio deviație peste pragul individual;
      - pe toată coloana: suma deviațiilor trebuie să fie ~0.

    A doua este cea care contează. Rotunjirea reală se compensează (unele
    apartamente în plus, altele în minus) și un prag individual singur lasă să
    treacă pe cineva care ciupește câțiva bani de la fiecare apartament. O
    deviație agregată sistematic într-o direcție nu e rotunjire, e transfer.
    """
    per_apartment = config.require_float(
        "reconciler.distribution_per_apartment_tolerance"
    )
    aggregate = config.require_float("reconciler.distribution_aggregate_tolerance")
    legal = config.require("reconciler.legal_references.R3")

    findings: list[Finding] = []
    verified: list[str] = []
    unverified: list[tuple[str, str]] = []
    apartments = payment.apartment_lines

    for expense in payment.expense_lines:
        weights, reason = distribution_weights(expense, apartments, config)
        if weights is None:
            unverified.append((expense.category, reason or "date insuficiente"))
            continue

        missing = [
            line.apartment_no
            for line in apartments
            if expense.category not in line.charges
        ]
        suspect = [
            line.apartment_no
            for line in apartments
            if resolution and resolution.suspect_charge(line.apartment_no, expense.category)
        ]
        if missing or suspect:
            blocked = sorted(set(missing) | set(suspect))
            unverified.append(
                (
                    expense.category,
                    (
                        "valori lipsă sau incerte la apartamentele "
                        f"{', '.join(blocked)}"
                    ),
                )
            )
            continue

        shares = expected_shares(expense.amount, weights)
        deviations = [
            (line.apartment_no, line.charges[expense.category] - share, share)
            for line, share in zip(apartments, shares)
        ]
        outliers = [
            (number, _round(delta), share)
            for number, delta, share in deviations
            if abs(delta) > per_apartment
        ]
        # Deviația agregată se calculează din valorile nerotunjite și se rotunjește
        # o singură dată, la final. Rotunjirea fiecărei deviații înainte de sumă
        # fabrică un drift de câțiva bani pe o coloană perfect corectă, exact
        # falsul pozitiv pe care regula ar trebui să-l excludă.
        drift = _round(sum(delta for _, delta, _ in deviations))
        verified.append(expense.category)

        if outliers:
            worst = max(outliers, key=lambda item: abs(item[1]))
            moved = _round(sum(abs(delta) for _, delta, _ in outliers) / 2)
            findings.append(
                Finding(
                    rule_id="R3",
                    severity="high",
                    finding_type="wrong_distribution_key",
                    category=expense.category,
                    expected_value=f"repartizare pe „{expense.distribution_key}”",
                    found_value=(
                        f"{len(outliers)} din {len(apartments)} apartamente "
                        f"deviază de la cheia declarată"
                    ),
                    amount_involved=moved,
                    apartment_no=worst[0],
                    legal_reference=legal,
                    message=(
                        f"„{expense.category}” este declarată cu cheia "
                        f"„{expense.distribution_key}”, dar {len(outliers)} din "
                        f"{len(apartments)} apartamente primesc altă sumă decât "
                        f"cea corespunzătoare cheii. Cea mai mare abatere: "
                        f"apartamentul {worst[0]}, {lei(worst[1], signed=True)} lei față de "
                        f"{lei(worst[2])} lei. Total redistribuit greșit: "
                        f"{lei(moved)} lei."
                    ),
                )
            )

        if abs(drift) > aggregate:
            findings.append(
                Finding(
                    rule_id="R3",
                    severity="high",
                    finding_type="systematic_distribution_drift",
                    category=expense.category,
                    expected_value=0.0,
                    found_value=drift,
                    amount_involved=abs(drift),
                    legal_reference=legal,
                    message=(
                        f"„{expense.category}”: suma deviațiilor pe toate cele "
                        f"{len(apartments)} apartamente este {lei(drift, signed=True)} lei, nu "
                        f"zero. Rotunjirea se compensează; o abatere agregată "
                        f"într-o singură direcție înseamnă că suma repartizată "
                        f"diferă de suma declarată."
                    ),
                )
            )

    total = len(payment.expense_lines)
    if not unverified:
        status: Literal["verificat", "parțial", "neverificabil"] = "verificat"
    elif verified:
        status = "parțial"
    else:
        status = "neverificabil"
    coverage = RuleCoverage(
        "R3",
        status,
        len(verified),
        total,
        None if not unverified else "; ".join(f"{c}: {r}" for c, r in unverified),
    )
    return findings, coverage, verified, unverified


def rule_r4(
    payment: PaymentList, config: Config, resolution: Resolution | None
) -> tuple[list[Finding], RuleCoverage, list[tuple[str, str]]]:
    """Penalizările nu pot depăși plafonul legal pe zi din restanță."""
    rate = config.require_float("reconciler.penalty_max_rate_per_day")
    days = config.require_int("reconciler.penalty_days")
    legal = config.require("reconciler.legal_references.R4")

    findings: list[Finding] = []
    excluded: list[tuple[str, str]] = []
    checked = 0

    for line in payment.apartment_lines:
        if resolution and (
            resolution.is_suspect(line.apartment_no, "penalties")
            or resolution.is_suspect(line.apartment_no, "arrears")
        ):
            excluded.append(
                (line.apartment_no, "restanță sau penalizare citită nesigur")
            )
            continue
        checked += 1
        cap = _round(line.arrears * rate * days)
        if line.penalties <= cap + 0.005:
            continue
        excess = _round(line.penalties - cap)
        applied = (
            line.penalties / (line.arrears * days) if line.arrears > 0 else float("inf")
        )
        applied_text = (
            f"{percent(applied * 100)}%/zi" if line.arrears > 0 else "fără restanță"
        )
        findings.append(
            Finding(
                rule_id="R4",
                severity="high",
                finding_type="penalty_over_legal_cap",
                apartment_no=line.apartment_no,
                expected_value=cap,
                found_value=line.penalties,
                amount_involved=excess,
                legal_reference=legal,
                message=(
                    f"Apartamentul {line.apartment_no}: penalizare de "
                    f"{lei(line.penalties)} lei la o restanță de "
                    f"{lei(line.arrears)} lei, adică {applied_text}. Plafonul "
                    f"legal pentru {days} de zile este {lei(cap)} lei "
                    f"({percent(rate * 100, 1)}%/zi). Suma percepută în plus: "
                    f"{lei(excess)} lei."
                ),
            )
        )

    total = len(payment.apartment_lines)
    status: Literal["verificat", "parțial", "neverificabil"]
    if not excluded:
        status = "verificat"
    elif checked:
        status = "parțial"
    else:
        status = "neverificabil"
    return (
        findings,
        RuleCoverage(
            "R4",
            status,
            checked,
            total,
            None
            if not excluded
            else f"excluse apartamentele {', '.join(n for n, _ in excluded)}",
        ),
        excluded,
    )


def rule_r5(
    payment: PaymentList, config: Config, resolution: Resolution | None
) -> tuple[list[Finding], RuleCoverage]:
    """Cheltuiala lunii curente repartizată pe apartamente == totalul general.

    Se scad restanțele și penalizările din `total_due`: sunt istoric, nu
    cheltuială a lunii curente. Fără scăderea asta, orice listă corectă în care
    un proprietar are datorii ar produce o constatare falsă.
    """
    tolerance = config.require_float("reconciler.apartment_total_tolerance")
    total = len(payment.apartment_lines)

    suspect = [
        line.apartment_no
        for line in payment.apartment_lines
        if resolution
        and any(
            resolution.is_suspect(line.apartment_no, name)
            for name in ("total_due", "arrears", "penalties")
        )
    ]
    if suspect:
        return [], RuleCoverage(
            "R5",
            "neverificabil",
            total - len(suspect),
            total,
            f"total de plată incert la apartamentele {', '.join(sorted(suspect))}",
        )

    current = _round(
        sum(
            line.total_due - line.arrears - line.penalties
            for line in payment.apartment_lines
        )
    )
    declared = payment.declared_totals.total_general
    delta = _round(current - declared)
    coverage = RuleCoverage("R5", "verificat", total, total)
    if abs(delta) <= tolerance:
        return [], coverage
    return [
        Finding(
            rule_id="R5",
            severity="high",
            finding_type="apartment_sum_mismatch",
            expected_value=declared,
            found_value=current,
            amount_involved=abs(delta),
            message=(
                f"Cheltuiala lunii curente repartizată pe cele {total} de "
                f"apartamente însumează {lei(current)} lei, față de totalul "
                f"general declarat de {lei(declared)} lei; diferență "
                f"{lei(delta, signed=True)} lei."
            ),
        )
    ], coverage


def rule_r6(payment: PaymentList) -> tuple[list[Finding], RuleCoverage]:
    """Numărul de apartamente găsite trebuie să fie cel declarat."""
    declared = payment.declared_totals.apartment_count
    found = len(payment.apartment_lines)
    coverage = RuleCoverage("R6", "verificat", found, found)
    if found == declared:
        return [], coverage
    return [
        Finding(
            rule_id="R6",
            severity="high",
            finding_type="apartment_count_mismatch",
            expected_value=declared,
            found_value=found,
            amount_involved=None,
            message=(
                f"Documentul declară {declared} de apartamente, dar tabelul de "
                f"repartizare conține {found}. Cheltuielile împărțite în părți "
                f"egale sau pe apartament sunt calculate pe o bază greșită."
            ),
        )
    ], coverage


def rule_r7(
    payment: PaymentList,
    config: Config,
    as_of: date | None,
    substantive: list[Finding],
) -> tuple[list[Finding], RuleCoverage]:
    """Fereastra de contestare de la art. 28 alin. (3): mai e deschisă?

    Nu e o regulă despre document, ci despre dreptul proprietarului. Constatările
    de mai sus îi dau temei să conteste modul de calcul al cotei; alin. (3) îi dă
    10 zile de la AFIȘAREA listei ca să o facă. Un audit care găsește 900 de lei
    lipsă și tace despre faptul că termenul a expirat acum o săptămână i-a
    furnizat proprietarului o nemulțumire, nu un drept.

    `as_of` se injectează, nu se citește din ceas: o regulă care depinde de ora
    rulării nu mai e reproductibilă.
    """
    total = 1
    if not substantive:
        return [], RuleCoverage(
            "R7", "verificat", total, total,
            "nicio constatare de contestat",
        )
    if as_of is None:
        return [], RuleCoverage(
            "R7", "neverificabil", 0, total,
            "data de referință nu a fost furnizată rulării",
        )
    if payment.posting_date is None:
        return [], RuleCoverage(
            "R7", "neverificabil", 0, total,
            "documentul nu conține data afișării la avizier, de la care curge "
            "termenul de contestare",
        )

    window = config.require_int("legal.art_28_3.contestation_window_days")
    response = config.require_int("legal.art_28_3.response_days")
    escalation = config.require_int("legal.art_28_4.unresolved_days")
    posted = date.fromisoformat(payment.posting_date)
    deadline = posted + timedelta(days=window)
    days_left = (deadline - as_of).days

    if days_left >= 0:
        return [
            Finding(
                rule_id="R7",
                severity="info",
                finding_type="contestation_window_open",
                expected_value=deadline.isoformat(),
                found_value=as_of.isoformat(),
                amount_involved=None,
                legal_reference=config.require("legal.art_28_3.reference"),
                message=(
                    f"Lista a fost afișată la {posted.isoformat()}. Contestația "
                    f"privind modul de calcul al cotei trebuie depusă până la "
                    f"{deadline.isoformat()} inclusiv. Mai sunt {days_left} "
                    f"zile. Președintele are obligația să răspundă în scris în "
                    f"{response} zile de la primire; dacă nu o face, sesizarea "
                    f"autorității locale devine disponibilă după {escalation} "
                    f"zile de la depunere."
                ),
            )
        ], RuleCoverage("R7", "verificat", total, total)

    return [
        Finding(
            rule_id="R7",
            severity="medium",
            finding_type="contestation_window_expired",
            expected_value=deadline.isoformat(),
            found_value=as_of.isoformat(),
            amount_involved=None,
            legal_reference=config.require("legal.art_28_1.reference"),
            message=(
                f"Lista a fost afișată la {posted.isoformat()}, iar termenul de "
                f"{window} zile pentru contestarea modului de calcul s-a închis "
                f"la {deadline.isoformat()}, acum {abs(days_left)} zile. Calea "
                f"contestației nu mai este deschisă pentru această listă. Rămâne "
                f"dreptul de a solicita copii după documentele asociației, care "
                f"nu are termen, iar constatările pot fi ridicate la contestarea "
                f"listei următoare."
            ),
        )
    ], RuleCoverage("R7", "verificat", total, total)


# --------------------------------------------------------------------------
# Orchestrare
# --------------------------------------------------------------------------


def _integrity_findings(resolution: Resolution | None) -> list[Finding]:
    """Incoerențele reconfirmate de o a doua citire devin constatări de audit."""
    if resolution is None:
        return []
    findings: list[Finding] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for issue in resolution.confirmed_inconsistencies:
        key = (issue.rule_id, issue.apartment_no, issue.category)
        if key in seen:
            continue
        seen.add(key)
        findings.append(_finding_from_issue(issue))
    return findings


def _finding_from_issue(issue: IntegrityIssue) -> Finding:
    return Finding(
        rule_id=issue.rule_id,
        severity="high",
        finding_type="internal_arithmetic_mismatch",
        apartment_no=issue.apartment_no,
        category=issue.category,
        expected_value=issue.expected,
        found_value=issue.found,
        amount_involved=abs(issue.delta),
        message=issue.message + " Citirea a fost confirmată de o a doua trecere.",
    )


def audit(
    payment: PaymentList,
    config: Config,
    resolution: Resolution | None = None,
    as_of: date | None = None,
) -> AuditResult:
    """Rulează toate regulile și întoarce constatările plus raportul de acoperire.

    `as_of` este data față de care se evaluează termenele procedurale (R7). Când
    lipsește, R7 nu se pronunță și spune asta în coverage report.
    """
    findings: list[Finding] = list(_integrity_findings(resolution))
    rules: list[RuleCoverage] = []

    findings += rule_r1(payment, config)
    rules.append(RuleCoverage("R1", "verificat", len(payment.expense_lines),
                              len(payment.expense_lines)))

    r2_findings, r2_coverage = rule_r2(payment, config, resolution)
    findings += r2_findings
    rules.append(r2_coverage)

    r3_findings, r3_coverage, verified, unverified = rule_r3(
        payment, config, resolution
    )
    findings += r3_findings
    rules.append(r3_coverage)

    r4_findings, r4_coverage, excluded = rule_r4(payment, config, resolution)
    findings += r4_findings
    rules.append(r4_coverage)

    r5_findings, r5_coverage = rule_r5(payment, config, resolution)
    findings += r5_findings
    rules.append(r5_coverage)

    r6_findings, r6_coverage = rule_r6(payment)
    findings += r6_findings
    rules.append(r6_coverage)

    r7_findings, r7_coverage = rule_r7(payment, config, as_of, list(findings))
    findings += r7_findings
    rules.append(r7_coverage)

    requests: list[str] = []
    if payment.posting_date is None:
        requests.append(
            "Confirmarea datei la care lista de plată a fost afișată la avizier: "
            "de la ea curge termenul de contestare a modului de calcul al cotei."
        )
    if unverified:
        requests.append(
            "Anexa cu consumurile individuale contorizate (index vechi, index "
            "nou, consum) pentru pozițiile repartizate pe consum."
        )
    requests.append(
        "Facturile furnizorilor pentru fiecare poziție de cheltuială: "
        "reconcilierea de față verifică doar coerența internă a listei, nu "
        "corespondența sumelor cu documentele justificative."
    )
    uncertain = sorted(
        {number for number, _ in excluded}
        | (resolution.unauditable_apartments if resolution else set())
    )
    if uncertain:
        requests.append(
            "Exemplarul original al listei de plată, lizibil, pentru "
            f"apartamentele la care transcrierea a rămas incertă: "
            f"{', '.join(uncertain)}."
        )

    coverage = CoverageReport(
        rules=rules,
        expense_lines_total=len(payment.expense_lines),
        expense_lines_verified=verified,
        expense_lines_unverified=unverified,
        apartments_total=len(payment.apartment_lines),
        apartments_excluded=excluded,
        documents_to_request=requests,
    )
    return AuditResult(findings=findings, coverage=coverage)

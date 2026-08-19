"""Extractor de liste de plată: PDF -> PaymentList validat.

Regula centrala a modulului: modelul TRANSCRIE, nu calculeaza. Nicio suma din
`PaymentList` nu este derivata aritmetic aici; fiecare valoare trebuie sa existe
tiparita in document. Un camp ilizibil, taiat sau ambiguu ajunge in
`extraction_confidence.low_confidence_fields`, niciodata ghicit.

Extragerea se face in trei treceri, fiecare cu schema ei restransa, pentru ca un
singur raspuns cu ~350 de numere transcrise degradeaza fidelitatea:
  1. antet, totaluri declarate si tabelul de cheltuieli pe categorii;
  2. tabelul de repartizare pe apartamente;
  3. anexa de consumuri contorizate, daca documentul o publica.

Rulare:
    python -m consilium.extractor samples/synthetic/sample_clean.pdf -o out.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

from consilium.config import Config
from consilium.integrity import (
    IntegrityIssue,
    IntegrityReport,
    Resolution,
    check_integrity,
    without_reread,
)
from consilium.schema import (
    ApartmentLine,
    Consumption,
    DeclaredTotals,
    DistributionKey,
    ExpenseLine,
    ExtractionConfidence,
    PaymentList,
)

MODEL = "gemini-3.5-flash"
DEFAULT_LOCATION = "global"
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 4.0

_ENV_CANDIDATES = [Path(".env"), Path("hoa_agent/.env")]


# --------------------------------------------------------------------------
# Scheme de sarma (model-facing)
#
# Nu expun `dict` catre API-ul de structured output si evita campurile
# nullable acolo unde nu e nevoie. Sunt convertite in schema publica din
# consilium.schema imediat dupa raspuns.
# --------------------------------------------------------------------------


class _WireExpenseLine(BaseModel):
    category: str
    amount: float
    distribution_key: DistributionKey
    source_invoice_ref: str


class _WireHeader(BaseModel):
    document_id: str
    association_ref: str
    period: str
    posting_date_printed: str
    total_general: float
    apartment_count: int
    expense_lines: list[_WireExpenseLine]
    low_confidence_fields: list[str]


class _WireCharge(BaseModel):
    category: str
    amount: float


class _WireApartment(BaseModel):
    apartment_no: str
    persons: int
    cota_indiviza: float
    charges: list[_WireCharge]
    arrears: float
    penalties: float
    total_due: float


class _WireApartments(BaseModel):
    apartment_lines: list[_WireApartment]
    low_confidence_fields: list[str]


class _WireConsumptionRow(BaseModel):
    apartment_no: str
    apa_rece_mc: float | None = None
    apa_calda_mc: float | None = None
    caldura_gcal: float | None = None


class _WireConsumption(BaseModel):
    annex_present: bool
    rows: list[_WireConsumptionRow]
    low_confidence_fields: list[str]


# --------------------------------------------------------------------------
# Prompturi
# --------------------------------------------------------------------------

SYSTEM_INSTRUCTION = """\
Ești un transcriptor de documente contabile românești. Sarcina ta este să copiezi
valori tipărite dintr-un PDF într-o structură JSON.

REGULI ABSOLUTE:
1. TRANSCRII, NU CALCULEZI. Nu adună, nu scădea, nu înmulți, nu împărți, nu
   deduce și nu completa nimic. Fiecare valoare returnată trebuie să existe
   tipărită în document, exact așa cum apare.
2. NU GHICI. Dacă o valoare este ilizibilă, tăiată, acoperită, ambiguă sau
   lipsește din document, NU o inventa și NU o deriva din alte valori. Adaugă
   calea câmpului în `low_confidence_fields` și pune valoarea cea mai apropiată
   de ce vezi, marcând-o acolo. Dacă nu vezi absolut nimic, pune 0 și
   OBLIGATORIU marchează câmpul.
3. NU CORECTA DOCUMENTUL. Dacă un total tipărit nu corespunde cu suma rândurilor,
   transcrii totalul tipărit. Inconsistențele sunt exact ce caută sistemul din
   aval; dacă le repari aici, auditul devine inutil.
4. Numerele sunt în format românesc: punctul separă miile, virgula separă
   zecimalele. "1.234,56" înseamnă 1234.56. Returnează-le ca numere JSON cu
   punct zecimal.
5. Nu inventa rânduri și nu omite rânduri. Numărul de rânduri returnate trebuie
   să fie exact numărul de rânduri din tabel.

Căile din `low_confidence_fields` folosesc notație cu punct și index, de exemplu:
`apartment_lines[12].penalties`, `expense_lines[3].amount`,
`declared_totals.total_general`.
"""

HEADER_PROMPT = """\
Transcrie ANTETUL și TABELUL DE CHELTUIELI PE CATEGORII din acest PDF.
Ignoră complet tabelul de repartizare pe apartamente și eventualele anexe.

Câmpuri:
- `document_id`: identificatorul documentului din antet (ex. câmpul "Document:").
- `association_ref`: denumirea asociației de proprietari.
- `period`: perioada în format YYYY-MM.
- `posting_date_printed`: data afișării listei la avizier, TRANSCRISĂ EXACT cum e
  tipărită (de ex. "05.12.2025"). Caută o etichetă de tipul "Data afișării",
  "Afișat la data de", "Data afișarii listei". Dacă documentul nu conține o
  astfel de dată, returnează șir gol. Nu o deduce din perioadă și nu o inventa.
- `total_general`: valoarea de pe rândul "TOTAL GENERAL" al tabelului de
  cheltuieli, EXACT cum e tipărită. Nu o recalcula din rânduri, chiar dacă nu
  corespunde.
- `apartment_count`: numărul de apartamente declarat în antet.
- `expense_lines`: câte o intrare per rând de categorie (fără rândul TOTAL).
  - `category`: eticheta categoriei, exact cum e scrisă.
  - `amount`: suma tipărită pe acel rând.
  - `distribution_key`: normalizează cheia de repartizare tipărită astfel:
      "cotă indiviză" / "cota indiviza" / "cotă-parte"   -> cota_indiviza
      "număr persoane" / "pe persoane"                   -> persoane
      "consum contorizat" / "pe consum" / "consum"       -> consum
      "părți egale" / "egal" / "pe apartament"           -> egal
    Aceasta este singura normalizare permisă. Dacă textul nu se potrivește
    niciunei variante, alege-o pe cea mai apropiată și marchează câmpul în
    `low_confidence_fields`.
  - `source_invoice_ref`: documentul justificativ de pe acel rând; șir gol dacă
    coloana lipsește sau celula e goală.
"""

APARTMENTS_PROMPT = """\
Transcrie TABELUL DE REPARTIZARE PE APARTAMENTE din acest PDF (secțiunea
"Repartizarea pe apartamente"). Tabelul se poate întinde pe mai multe pagini, cu
antetul repetat; parcurge-l integral.

Returnează câte o intrare per apartament, în ordinea din document. NU include
rândul final "TOTAL" — acela nu este un apartament.

Pentru fiecare apartament:
- `apartment_no`: numărul apartamentului, ca text.
- `persons`: numărul de persoane.
- `cota_indiviza`: cota indiviză în procente, ca număr (ex. "4,63" -> 4.63).
- `charges`: câte o intrare pentru FIECARE coloană de categorie de cheltuială.
  `amount` este valoarea din celulă, copiată ca atare.
  Pentru `category` folosește EXACT una dintre etichetele de mai jos, preluate
  din tabelul de cheltuieli al aceluiași document:
{categories}
  Antetul coloanei poate fi prescurtat sau despărțit în silabe (de ex. coloana
  "Apă caldă" corespunde etichetei "Apă caldă menajeră", iar "Salubri-tate"
  corespunde etichetei "Salubritate"). Potrivește fiecare coloană cu eticheta ei
  și returnează eticheta din listă, nu antetul coloanei. Aceasta este o
  potrivire de identificatori, nu o modificare a valorilor.
  Dacă o coloană de cheltuială nu corespunde niciunei etichete din listă,
  folosește antetul coloanei ca atare și marchează câmpul în
  `low_confidence_fields`.
  NU include în `charges` coloanele "Total lună curentă", "Restanțe",
  "Penalizări" sau "TOTAL DE PLATĂ" — acelea au câmpurile lor.
- `arrears`: valoarea din coloana "Restanțe".
- `penalties`: valoarea din coloana "Penalizări".
- `total_due`: valoarea din coloana "TOTAL DE PLATĂ".

Toate cele patru valori de mai sus se copiază din celule. Nu le recalcula.
"""

CONSUMPTION_PROMPT = """\
Caută în acest PDF o ANEXĂ CU CONSUMURI CONTORIZATE INDIVIDUAL (coloane de tip
"index vechi", "index nou", "consum (mc)", "Gcal", pe apartament).

- Dacă documentul NU conține o astfel de anexă, returnează `annex_present`:
  false și `rows`: listă goală. Nu deduce consumurile din sumele de plată și nu
  le inventa.
- Dacă anexa există, returnează `annex_present`: true și câte un rând per
  apartament, cu valorile din coloanele de CONSUM (nu indecșii):
  - `apa_rece_mc`: consumul de apă rece în metri cubi.
  - `apa_calda_mc`: consumul de apă caldă în metri cubi.
  - `caldura_gcal`: consumul de energie termică în Gcal.
  Orice câmp pentru care documentul nu are coloană rămâne null.
  NU include rândul "TOTAL".
"""


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


def _load_env() -> None:
    for candidate in _ENV_CANDIDATES:
        if candidate.exists():
            load_dotenv(candidate, override=False)


def build_client() -> genai.Client:
    """Creeaza clientul Vertex AI. Fail-fast daca proiectul nu e configurat."""
    _load_env()
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT nu este setat. Adauga-l in .env sau in mediu."
        )
    location = os.environ.get("CONSILIUM_VERTEX_LOCATION", DEFAULT_LOCATION)
    return genai.Client(vertexai=True, project=project, location=location)


_WireT = TypeVar("_WireT", bound=BaseModel)


def _run_pass(
    client: genai.Client,
    pdf_part: types.Part,
    prompt: str,
    wire_model: type[_WireT],
) -> _WireT:
    """O trecere de extractie, cu reincercare la eroare de retea sau schema."""
    config = types.GenerateContentConfig(
        temperature=0.0,
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=wire_model,
    )
    contents = [
        types.Content(
            role="user", parts=[pdf_part, types.Part.from_text(text=prompt)]
        )
    ]

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model=MODEL, contents=contents, config=config
            )
            if response.parsed is not None:
                return response.parsed  # type: ignore[return-value]
            return wire_model.model_validate_json(response.text or "")
        except Exception as error:  # noqa: BLE001 - retry pe retea si pe schema
            last_error = error
            if attempt == MAX_ATTEMPTS:
                break
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError(
        f"Extractia {wire_model.__name__} a esuat dupa {MAX_ATTEMPTS} incercari: "
        f"{last_error}"
    ) from last_error


# --------------------------------------------------------------------------
# Asamblare
# --------------------------------------------------------------------------


_DATE_FORMATS = ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d")


def normalize_posting_date(printed: str) -> str | None:
    """Aduce data afisarii la ISO. Conversia e determinista, facuta in cod.

    Modelul transcrie ce vede; interpretarea formatului nu se lasa pe seama lui.
    Un format nerecunoscut intoarce None si campul ajunge marcat, nu ghicit.
    """
    text = printed.strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            # Data afișării e o dată calendaristică locală, fără oră și fără
            # fus: nu are sens să o facem tz-aware.
            return datetime.strptime(text, fmt).date().isoformat()  # noqa: DTZ007
        except ValueError:
            continue
    return None


def _normalize_label(text: str) -> str:
    """Forma canonica a unei etichete de categorie, pentru potrivire.

    Elimina diacriticele, spatiile, cratimele si diferentele de registru. Nu
    atinge nicio valoare numerica: serveste exclusiv la legarea coloanelor din
    tabelul de apartamente de randurile din tabelul de cheltuieli.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in stripped.casefold() if ch.isalnum())


def align_charge_keys(
    apartments: list[ApartmentLine], categories: list[str]
) -> list[str]:
    """Leaga cheile din `charges` de etichetele din `expense_lines`.

    Antetele de coloana sunt frecvent prescurtate fata de randul de cheltuiala
    ("Apă caldă" pentru "Apă caldă menajeră"). Potrivirea se face aici, in cod
    determinist, nu se lasa pe seama modelului. Cheile care raman nepotrivite
    sau ambigue sunt returnate ca `low_confidence_fields`, nu forjate.
    """
    canonical = {_normalize_label(category): category for category in categories}
    unresolved: list[str] = []

    for index, apartment in enumerate(apartments):
        realigned: dict[str, float] = {}
        for key, value in apartment.charges.items():
            normalized = _normalize_label(key)
            target = canonical.get(normalized)
            if target is None:
                prefixes = [
                    category
                    for candidate, category in canonical.items()
                    if candidate.startswith(normalized)
                    or normalized.startswith(candidate)
                ]
                target = prefixes[0] if len(prefixes) == 1 else None
            if target is None:
                unresolved.append(f"apartment_lines[{index}].charges[{key!r}]")
                realigned[key] = value
            else:
                realigned[target] = value
        apartment.charges = realigned

    return unresolved


def _merge_consumption(
    apartments: list[ApartmentLine], wire: _WireConsumption
) -> None:
    """Ataseaza consumurile pe apartamente. Fara anexa, raman None."""
    if not wire.annex_present or not wire.rows:
        return
    by_number = {row.apartment_no.strip(): row for row in wire.rows}
    for apartment in apartments:
        row = by_number.get(apartment.apartment_no.strip())
        if row is None:
            continue
        consumption = Consumption(
            apa_rece_mc=row.apa_rece_mc,
            apa_calda_mc=row.apa_calda_mc,
            caldura_gcal=row.caldura_gcal,
        )
        apartment.consumption = None if consumption.is_empty() else consumption


def extract(pdf_path: str | Path, client: genai.Client | None = None) -> PaymentList:
    """Extrage o lista de plata dintr-un PDF si o valideaza contra schemei."""
    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF inexistent: {path}")

    client = client or build_client()
    pdf_part = types.Part.from_bytes(
        data=path.read_bytes(), mime_type="application/pdf"
    )

    header = _run_pass(client, pdf_part, HEADER_PROMPT, _WireHeader)

    # Trecerea 2 primeste etichetele deja citite in trecerea 1, ca sa cheile din
    # `charges` sa se lege de `expense_lines` fara potriviri aproximative in aval.
    category_list = "\n".join(
        f"    - {line.category.strip()}" for line in header.expense_lines
    )
    apartments_wire = _run_pass(
        client,
        pdf_part,
        APARTMENTS_PROMPT.format(categories=category_list),
        _WireApartments,
    )
    consumption_wire = _run_pass(
        client, pdf_part, CONSUMPTION_PROMPT, _WireConsumption
    )

    expense_lines = [
        ExpenseLine(
            category=line.category.strip(),
            amount=line.amount,
            distribution_key=line.distribution_key,
            source_invoice_ref=line.source_invoice_ref.strip() or None,
        )
        for line in header.expense_lines
    ]

    apartment_lines = [
        ApartmentLine(
            apartment_no=wire.apartment_no.strip(),
            persons=wire.persons,
            cota_indiviza=wire.cota_indiviza,
            charges={charge.category.strip(): charge.amount for charge in wire.charges},
            arrears=wire.arrears,
            penalties=wire.penalties,
            total_due=wire.total_due,
        )
        for wire in apartments_wire.apartment_lines
    ]
    _merge_consumption(apartment_lines, consumption_wire)

    unmatched = align_charge_keys(
        apartment_lines, [line.category for line in expense_lines]
    )

    low_confidence = list(
        dict.fromkeys(
            header.low_confidence_fields
            + apartments_wire.low_confidence_fields
            + consumption_wire.low_confidence_fields
            + unmatched
        )
    )

    posting_date = normalize_posting_date(header.posting_date_printed)
    if header.posting_date_printed.strip() and posting_date is None:
        low_confidence.append("posting_date")

    return PaymentList(
        document_id=header.document_id.strip(),
        association_ref=header.association_ref.strip(),
        period=header.period.strip(),
        posting_date=posting_date,
        declared_totals=DeclaredTotals(
            total_general=header.total_general,
            apartment_count=header.apartment_count,
        ),
        expense_lines=expense_lines,
        apartment_lines=apartment_lines,
        extraction_confidence=ExtractionConfidence(
            low_confidence_fields=low_confidence
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", help="calea catre PDF-ul listei de plata")
    parser.add_argument("-o", "--out", help="fisier JSON de iesire (implicit: stdout)")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="ruleaza R0 si recitirea tintita a zonelor picate",
    )
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    client = build_client()
    payment = extract(args.pdf, client=client)

    if args.verify:
        config = Config.load(args.config)
        report = check_integrity(payment, config)
        resolution = resolve_integrity(payment, report, config, args.pdf, client)
        print(
            f"R0: {len(report.issues)} incoerente, "
            f"{resolution.reread_calls} recitiri, "
            f"{len(resolution.unauditable_apartments)} apartamente neauditabile",
            file=sys.stderr,
        )

    payload = payment.model_dump(mode="json")
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"scris {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --------------------------------------------------------------------------
# Recitire țintită (R0)
#
# Când R0 pică, nu știm dacă documentul e inconsistent sau dacă noi am citit
# prost. Singurul mod de a afla este să citim din nou, izolat, doar zona
# implicată — la rezoluție mai mare și cu un răspuns mult mai mic decât o
# transcriere de tabel întreg.
# --------------------------------------------------------------------------


class _WireCellReread(BaseModel):
    found: bool
    amount: float
    low_confidence_fields: list[str]


class _WireRowReread(BaseModel):
    found: bool
    charges: list[_WireCharge]
    arrears: float
    penalties: float
    total_due: float
    low_confidence_fields: list[str]


class _WireColumnCell(BaseModel):
    apartment_no: str
    amount: float


class _WireColumnReread(BaseModel):
    found: bool
    cells: list[_WireColumnCell]
    low_confidence_fields: list[str]


CELL_REREAD_PROMPT = """\
Imaginea este O SINGURĂ PAGINĂ dintr-o listă de plată.

Caută în tabelul de repartizare rândul apartamentului {apartment}. Pe acel rând,
citește celula din coloana „{category}".

- Dacă apartamentul {apartment} NU apare pe această pagină, returnează
  `found`: false și `amount`: 0. Nu ghici.
- Dacă apare, returnează `found`: true și `amount` = valoarea tipărită în acea
  celulă, exact cum e scrisă.

Nu aduna, nu deduce din alte celule și nu completa din context. Dacă vreo cifră
nu se distinge cu certitudine, dă cea mai bună citire ȘI adaugă
"apartment_lines[{apartment}].charges['{category}']" în `low_confidence_fields`.
"""

ROW_REREAD_PROMPT = """\
Imaginea este O SINGURĂ PAGINĂ dintr-o listă de plată.

Caută în tabelul de repartizare rândul apartamentului {apartment} și transcrie-l
integral.

- Dacă apartamentul {apartment} NU apare pe această pagină, returnează
  `found`: false, liste goale și zerouri. Nu ghici.
- Dacă apare, returnează `found`: true și:
  - `charges`: câte o intrare pentru fiecare coloană de cheltuială, cu
    `category` din lista de mai jos și `amount` din celulă:
{categories}
  - `arrears`: coloana "Restanțe"; `penalties`: coloana "Penalizări";
    `total_due`: coloana "TOTAL DE PLATĂ".

Citește celulă cu celulă, urmărind cu atenție linia rândului: pagina poate fi
înclinată, iar o valoare de pe rândul vecin este o eroare gravă. Nu calcula
nimic. Orice celulă nesigură se marchează în `low_confidence_fields`.
"""

COLUMN_REREAD_PROMPT = """\
Imaginea este O SINGURĂ PAGINĂ dintr-o listă de plată.

Transcrie coloana „{category}" din tabelul de repartizare, pentru TOATE
apartamentele care apar pe această pagină.

- Dacă pe această pagină nu există tabelul de apartamente sau nu există coloana
  „{category}", returnează `found`: false și `cells`: listă goală.
- Altfel `found`: true și câte o intrare per apartament, cu `apartment_no` din
  prima coloană și `amount` din celula coloanei „{category}".

Nu include rândul "TOTAL". Nu calcula nimic. Celulele nesigure se marchează în
`low_confidence_fields`.
"""


def render_pages(pdf_path: str | Path, dpi: int) -> list[bytes]:
    """Randează fiecare pagină a PDF-ului ca PNG, la rezoluția cerută."""
    source = Path(pdf_path)
    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        result = subprocess.run(
            [
                "pdftoppm", "-r", str(dpi), "-png",
                str(source), str(work_dir / "page"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"pdftoppm a esuat pentru {source}: {result.stderr.strip()}"
            )
        pages = sorted(work_dir.glob("page-*.png"))
        if not pages:
            raise RuntimeError(f"pdftoppm nu a produs nicio pagina pentru {source}")
        return [page.read_bytes() for page in pages]


def _page_part(png: bytes) -> types.Part:
    return types.Part.from_bytes(data=png, mime_type="image/png")


def reread_cell(
    client: genai.Client, pages: list[bytes], apartment_no: str, category: str
) -> tuple[float | None, list[str], int]:
    """Recitește o singură celulă. Întoarce (valoare, marcaje, apeluri făcute)."""
    prompt = CELL_REREAD_PROMPT.format(apartment=apartment_no, category=category)
    calls = 0
    for png in pages:
        calls += 1
        answer = _run_pass(client, _page_part(png), prompt, _WireCellReread)
        if answer.found:
            return answer.amount, answer.low_confidence_fields, calls
    return None, [], calls


def reread_row(
    client: genai.Client, pages: list[bytes], apartment_no: str, categories: list[str]
) -> tuple[_WireRowReread | None, int]:
    """Recitește un rând întreg de apartament."""
    prompt = ROW_REREAD_PROMPT.format(
        apartment=apartment_no,
        categories="\n".join(f"    - {category}" for category in categories),
    )
    calls = 0
    for png in pages:
        calls += 1
        answer = _run_pass(client, _page_part(png), prompt, _WireRowReread)
        if answer.found:
            return answer, calls
    return None, calls


def reread_column(
    client: genai.Client, pages: list[bytes], category: str
) -> tuple[dict[str, float], list[str], int]:
    """Recitește o coloană de categorie de pe toate paginile."""
    prompt = COLUMN_REREAD_PROMPT.format(category=category)
    values: dict[str, float] = {}
    flags: list[str] = []
    calls = 0
    for png in pages:
        calls += 1
        answer = _run_pass(client, _page_part(png), prompt, _WireColumnReread)
        if not answer.found:
            continue
        for cell in answer.cells:
            values.setdefault(cell.apartment_no.strip(), cell.amount)
        flags.extend(answer.low_confidence_fields)
    return values, flags, calls


def _close(left: float, right: float, tolerance: float) -> bool:
    return abs(left - right) <= tolerance


def resolve_integrity(
    payment: PaymentList,
    report: IntegrityReport,
    config: Config,
    pdf_path: str | Path,
    client: genai.Client | None = None,
) -> Resolution:
    """Recitește zonele picate la R0 și decide ce e eroare de citire, ce e de audit.

    A doua citire confirmă prima  -> documentul chiar e inconsistent, e constatare
                                     de audit și rândul rămâne auditabil.
    A doua citire diferă          -> nu știm care e corectă. Câmpurile diferite
                                     intră în low_confidence_fields, iar rândul
                                     devine neauditabil. Nu adoptăm a doua
                                     citire: o valoare nesigură rămâne nesigură
                                     chiar dacă închide aritmetica.
    """
    if report.is_clean:
        return Resolution(report=report)
    if client is None:
        return without_reread(report)

    tolerance = config.require_float("integrity.reread_match_tolerance")
    pages = render_pages(pdf_path, config.require_int("integrity.reread_dpi"))
    categories = [line.category for line in payment.expense_lines]
    by_number = {line.apartment_no: line for line in payment.apartment_lines}

    confirmed: list[IntegrityIssue] = []
    unauditable_apartments: set[str] = set()
    unauditable_categories: set[str] = set()
    flags: list[str] = []
    calls = 0

    explained_rows = {cell.apartment_no for cell in report.suspect_cells}
    explained_columns = {cell.category for cell in report.suspect_cells}

    # 1. Celule localizate prin intersecția rând x coloană: cea mai ieftină recitire.
    for cell in report.suspect_cells:
        line = by_number.get(cell.apartment_no)
        if line is None or cell.category not in line.charges:
            unauditable_apartments.add(cell.apartment_no)
            continue
        original = line.charges[cell.category]
        second, cell_flags, made = reread_cell(
            client, pages, cell.apartment_no, cell.category
        )
        calls += made
        flags.extend(cell_flags)
        path = f"apartment_lines[{cell.apartment_no}].charges[{cell.category!r}]"
        if second is None or not _close(second, original, tolerance):
            flags.append(path)
            unauditable_apartments.add(cell.apartment_no)
            unauditable_categories.add(cell.category)
        else:
            confirmed.extend(
                issue
                for issue in report.issues
                if issue.apartment_no == cell.apartment_no
                or issue.category == cell.category
            )

    # 2. Rânduri picate fără o coloană care să le explice: se recitește tot rândul.
    for issue in report.row_issues:
        number = issue.apartment_no
        if number is None or number in explained_rows:
            continue
        line = by_number.get(number)
        if line is None:
            continue
        second, made = reread_row(client, pages, number, categories)
        calls += made
        if second is None:
            flags.append(f"apartment_lines[{number}]")
            unauditable_apartments.add(number)
            continue
        flags.extend(second.low_confidence_fields)
        second_charges = {charge.category: charge.amount for charge in second.charges}
        differing = [
            f"apartment_lines[{number}].charges[{category!r}]"
            for category, amount in line.charges.items()
            if category not in second_charges
            or not _close(second_charges[category], amount, tolerance)
        ]
        differing += [
            f"apartment_lines[{number}].{name}"
            for name, original in (
                ("arrears", line.arrears),
                ("penalties", line.penalties),
                ("total_due", line.total_due),
            )
            if not _close(getattr(second, name), original, tolerance)
        ]
        if differing:
            flags.extend(differing)
            unauditable_apartments.add(number)
        else:
            confirmed.append(issue)

    # 3. Coloane picate fără o celulă care să le explice.
    for issue in report.column_issues:
        category = issue.category
        if category is None or category in explained_columns:
            continue
        values, column_flags, made = reread_column(client, pages, category)
        calls += made
        flags.extend(column_flags)
        differing = [
            line.apartment_no
            for line in payment.apartment_lines
            if line.apartment_no not in values
            or category not in line.charges
            or not _close(values[line.apartment_no], line.charges[category], tolerance)
        ]
        if differing:
            flags.extend(
                f"apartment_lines[{number}].charges[{category!r}]"
                for number in differing
            )
            unauditable_apartments.update(differing)
            unauditable_categories.add(category)
        else:
            confirmed.append(issue)

    merged = list(
        dict.fromkeys(payment.extraction_confidence.low_confidence_fields + flags)
    )
    payment.extraction_confidence.low_confidence_fields = merged

    return Resolution(
        report=report,
        confirmed_inconsistencies=list(dict.fromkeys(confirmed)),
        unauditable_apartments=unauditable_apartments,
        unauditable_categories=unauditable_categories,
        low_confidence_fields=list(dict.fromkeys(flags)),
        reread_calls=calls,
    )

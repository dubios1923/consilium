"""Contractul de date al Consilium.

Acesta este singurul modul pe care il importa si extractorul, si reconciler-ul.
Nu contine logica de retea si nu depinde de niciun SDK de model: reconciler-ul
trebuie sa poata rula complet offline.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

DistributionKey = Literal["cota_indiviza", "persoane", "consum", "egal"]

PERIOD_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
DATE_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"


class StrictModel(BaseModel):
    """Baza comuna: campuri necunoscute sunt o eroare, nu zgomot ignorat."""

    model_config = ConfigDict(extra="forbid")


class DeclaredTotals(StrictModel):
    """Totalurile asa cum sunt tiparite in document, nu recalculate."""

    total_general: float = Field(
        description="Totalul general declarat in tabelul de cheltuieli."
    )
    apartment_count: int = Field(
        ge=1, description="Numarul de apartamente declarat in antet."
    )


class ExpenseLine(StrictModel):
    """O pozitie din tabelul de cheltuieli pe categorii."""

    category: str
    amount: float
    distribution_key: DistributionKey
    source_invoice_ref: str | None = None


class Consumption(StrictModel):
    """Consum individual contorizat, daca documentul il publica.

    Campurile raman None cand documentul nu contine anexa de consumuri. In acel
    caz pozitiile repartizate pe consum devin neverificabile si trebuie sa apara
    in coverage report, nu sa fie ignorate.
    """

    apa_rece_mc: float | None = None
    apa_calda_mc: float | None = None
    caldura_gcal: float | None = None

    def is_empty(self) -> bool:
        return (
            self.apa_rece_mc is None
            and self.apa_calda_mc is None
            and self.caldura_gcal is None
        )


class ApartmentLine(StrictModel):
    """Un rand din tabelul de repartizare pe apartamente."""

    apartment_no: str
    persons: int = Field(ge=0)
    cota_indiviza: float
    charges: dict[str, float] = Field(
        default_factory=dict,
        description="Suma pe fiecare categorie; cheile sunt etichetele "
        "categoriilor exact cum apar in expense_lines.",
    )
    consumption: Consumption | None = None
    arrears: float
    penalties: float
    total_due: float


class ExtractionConfidence(StrictModel):
    """Ce nu a putut fi citit cu certitudine.

    Extractorul nu ghiceste niciodata: un camp ilizibil, taiat sau ambiguu se
    trece aici, cu calea lui in document.
    """

    low_confidence_fields: list[str] = Field(default_factory=list)


class PaymentList(StrictModel):
    """Lista de plata transcrisa dintr-un PDF, fara nicio valoare derivata."""

    document_id: str
    association_ref: str
    period: Annotated[str, Field(pattern=PERIOD_PATTERN)]
    posting_date: Annotated[str | None, Field(pattern=DATE_PATTERN)] = Field(
        default=None,
        description="Data afisarii listei la avizier, ISO YYYY-MM-DD. De la ea "
        "curge termenul de contestare de la art. 28 alin. (3) din Legea "
        "196/2018; fara ea nu se poate spune daca dreptul mai e deschis.",
    )
    declared_totals: DeclaredTotals
    expense_lines: list[ExpenseLine]
    apartment_lines: list[ApartmentLine]
    extraction_confidence: ExtractionConfidence = Field(
        default_factory=ExtractionConfidence
    )

    def has_consumption_data(self) -> bool:
        return any(
            line.consumption is not None and not line.consumption.is_empty()
            for line in self.apartment_lines
        )

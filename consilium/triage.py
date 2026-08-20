"""Gate de intrare: merită documentul ăsta pipeline-ul scump?

Extracția costă ~82 de secunde de Gemini per document, în trei treceri. Un PDF
care nu e listă de plată consumă tot bugetul ca să eșueze abia la validarea
schemei. Un model mic decide întâi DACĂ merită rulat pipeline-ul.

Gate-ul e o optimizare de cost, nu o regulă de audit. Din asta decurge singura
lui proprietate importantă: **eșuează deschis**. Dacă modelul de triaj nu
răspunde, răspunde prost sau nu e configurat, pipeline-ul continuă. O optimizare
care poate bloca un audit valid e mai scumpă decât secundele pe care le salvează.

Vede doar prima pagină, randată ca imagine: e suficient pentru a distinge o listă
de plată de o hotărâre AGA, și e cea mai ieftină întrebare posibilă.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from consilium.config import Config

TriageStatus = Literal["accepted", "rejected", "unavailable"]
Confidence = Literal["high", "medium", "low"]


class TriageVerdict(BaseModel):
    """Ce întoarce modelul de triaj."""

    is_payment_list: bool
    document_type: str
    confidence: Confidence
    reason: str


@dataclass(frozen=True)
class TriageOutcome:
    """Decizia gate-ului, în forma pe care o consumă pipeline-ul."""

    is_payment_list: bool
    status: TriageStatus
    document_type: str = ""
    confidence: str = ""
    reason: str = ""
    model: str = ""
    error: str | None = None

    @property
    def should_continue(self) -> bool:
        """Continuăm dacă documentul e acceptat SAU dacă triajul n-a putut decide."""
        return self.status != "rejected"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "is_payment_list": self.is_payment_list,
            "document_type": self.document_type,
            "confidence": self.confidence,
            "reason": self.reason,
            "model": self.model,
            "error": self.error,
        }


SYSTEM_INSTRUCTION = """\
Ești un filtru de rutare a documentelor. Primești prima pagină a unui document
românesc și decizi dacă este o LISTĂ DE PLATĂ a unei asociații de proprietari.

O listă de plată are, tipic: un antet cu asociația și perioada (lună/an), un
tabel de cheltuieli pe categorii (apă, căldură, salubritate, lift, administrare,
fond de reparații) și un tabel cu apartamentele, cu sume de plată pe fiecare.

NU sunt liste de plată, chiar dacă vin de la aceeași asociație: hotărâri ale
adunării generale, procese-verbale, convocatoare, contracte, facturi de la
furnizori, chitanțe, situații financiare anuale, regulamente, notificări.

Răspunde strict cu structura cerută. `reason` are cel mult o propoziție, în
română. Fii conservator: dacă pagina pare a fi un tabel de plăți pe apartamente
dar nu ești sigur, răspunde `true` cu `confidence` scăzut. Costul unui fals
negativ (un audit valid respins) este mult mai mare decât al unui fals pozitiv.
"""

PROMPT = """\
Este această pagină prima pagină a unei liste de plată a unei asociații de
proprietari?

- `is_payment_list`: true sau false.
- `document_type`: ce este de fapt documentul, pe scurt (de ex. "listă de plată",
  "hotărâre a adunării generale", "factură furnizor", "proces-verbal").
- `confidence`: high, medium sau low.
- `reason`: o propoziție care justifică decizia, în română.
"""


def render_first_page(pdf_path: str | Path, dpi: int) -> bytes:
    """Randează doar prima pagină. Restul documentului nu interesează gate-ul."""
    source = Path(pdf_path)
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        result = subprocess.run(
            [
                "pdftoppm", "-r", str(dpi), "-png",
                "-f", "1", "-l", "1",
                str(source), str(prefix),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"pdftoppm a eșuat: {result.stderr.strip()}")
        pages = sorted(Path(tmp).glob("page*.png"))
        if not pages:
            raise RuntimeError(f"nicio pagină randată din {source}")
        return pages[0].read_bytes()


def triage(
    pdf_path: str | Path,
    config: Config,
    client: Any = None,
) -> TriageOutcome:
    """Decide dacă documentul merită pipeline-ul. Nu ridică niciodată.

    Orice problemă — model indisponibil, pagină nerandabilă, răspuns invalid —
    întoarce `status="unavailable"` cu `is_payment_list=True`, adică „nu știu,
    treci mai departe”.
    """
    model = config.require("triage.model")
    if not config.require("triage.enabled"):
        return TriageOutcome(
            is_payment_list=True,
            status="unavailable",
            reason="triaj dezactivat din configurație",
            model=model,
        )

    try:
        from google.genai import types

        if client is None:
            from consilium.extractor import build_client

            client = build_client()

        page = render_first_page(pdf_path, config.require_int("triage.dpi"))
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(data=page, mime_type="image/png"),
                        types.Part.from_text(text=PROMPT),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=TriageVerdict,
                max_output_tokens=config.require_int("triage.max_output_tokens"),
            ),
        )
        verdict = response.parsed
        if verdict is None:
            verdict = TriageVerdict.model_validate_json(response.text or "")

        return TriageOutcome(
            is_payment_list=verdict.is_payment_list,
            status="accepted" if verdict.is_payment_list else "rejected",
            document_type=verdict.document_type.strip(),
            confidence=verdict.confidence,
            reason=verdict.reason.strip(),
            model=model,
        )
    except Exception as error:  # noqa: BLE001 - gate-ul eșuează deschis
        return TriageOutcome(
            is_payment_list=True,
            status="unavailable",
            reason="triajul nu a putut decide; documentul continuă în pipeline",
            model=model,
            error=f"{type(error).__name__}: {error}",
        )

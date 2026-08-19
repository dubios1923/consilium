"""UI de inspecție pentru auditurile Consilium.

Read-only prin construcție: agentul acesta citește din Firestore și atât. Nu
declanșează audituri, nu rescrie stări, nu regenerează artefacte. Pipeline-ul
rulează detașat, în Cloud Run Job; dacă inspecția ar putea și executa, un „mai
rulează o dată” dintr-o conversație ar porni procesări în paralel pe același
document.

Configurația de deploy a serviciului rămâne neatinsă.
"""

from __future__ import annotations

import os
from typing import Any

from google.adk.agents import Agent

COLLECTION = "audits"


def _store():
    from consilium.state import FirestoreAuditStore

    return FirestoreAuditStore(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))


def _summary(record: Any) -> dict[str, Any]:
    return {
        "audit_id": record.audit_id,
        "status": record.status,
        "association_ref": record.association_ref,
        "period": record.period,
        "document_id": record.document_id,
        "source_uri": record.source_uri,
        "findings_count": len(record.findings),
        "artifacts_count": len(record.artifact_uris),
        "error": record.error,
    }


def list_audits(limit: int = 20) -> dict:
    """Listează cele mai recente audituri, cu starea fiecăruia.

    Args:
        limit: câte audituri să întoarcă (implicit 20).

    Returns:
        dict: status și lista auditurilor, cel mai recent primul.
    """
    try:
        records = _store().list_recent(limit=limit)
    except Exception as error:  # noqa: BLE001
        return {"status": "error", "error_message": str(error)}
    return {
        "status": "success",
        "count": len(records),
        "audits": [_summary(record) for record in records],
    }


def get_audit(audit_id: str) -> dict:
    """Arată un audit complet: etape, constatări, acoperire, artefacte.

    Args:
        audit_id: identificatorul auditului, de forma `aud-...`.

    Returns:
        dict: status și documentul auditului, sau un mesaj de eroare.
    """
    try:
        record = _store().get(audit_id)
    except Exception as error:  # noqa: BLE001
        return {"status": "error", "error_message": str(error)}
    if record is None:
        return {"status": "error", "error_message": f"audit inexistent: {audit_id}"}
    return {
        "status": "success",
        "audit": _summary(record),
        "step_log": record.step_log,
        "findings": record.findings,
        "coverage_report": record.coverage_report,
        "artifact_uris": record.artifact_uris,
    }


def get_association_history(association_ref: str) -> dict:
    """Istoricul auditurilor unei asociații, în ordinea perioadelor.

    Args:
        association_ref: denumirea asociației, exact cum apare în listele de plată.

    Returns:
        dict: status și auditurile asociației, de la cel mai vechi la cel mai nou.
    """
    try:
        records = _store().history(association_ref)
    except Exception as error:  # noqa: BLE001
        return {"status": "error", "error_message": str(error)}
    return {
        "status": "success",
        "association_ref": association_ref,
        "count": len(records),
        "audits": [_summary(record) for record in records],
    }


root_agent = Agent(
    name="hoa_agent",
    model="gemini-3.5-flash",
    description=(
        "Consultă auditurile Consilium asupra listelor de plată ale asociațiilor "
        "de proprietari. Doar citire."
    ),
    instruction=(
        "Ajuți un proprietar să înțeleagă rezultatul auditurilor deja rulate.\n\n"
        "Poți doar CITI. Nu poți porni, relua sau modifica un audit; auditurile "
        "se declanșează automat la încărcarea unei liste de plată în bucket-ul de "
        "intrare. Dacă ți se cere să rulezi ceva, explică asta.\n\n"
        "Când prezinți un audit: spune întâi starea, apoi constatările cu suma "
        "implicată și temeiul legal, apoi OBLIGATORIU ce nu s-a putut verifica "
        "din coverage_report. Un audit prezentat fără punctele lui oarbe induce "
        "în eroare.\n\n"
        "Nu recalcula nicio sumă și nu deduce constatări noi din cele existente. "
        "Cifrele sunt cele din Firestore; dacă ceva lipsește, spune că lipsește."
    ),
    tools=[list_audits, get_audit, get_association_history],
)

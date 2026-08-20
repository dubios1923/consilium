"""Starea unui audit în Firestore, colecția `audits`.

Un document per caz. Pipeline-ul rulează detașat, fără nimeni care să se uite la
el, deci fiecare tranziție trebuie să lase urmă: dacă etapa a treia moare la ora
trei dimineața, `step_log` spune exact unde și de ce.

Identitatea documentului e derivată determinist din sursă, nu generată aleator:
Eventarc livrează același eveniment de mai multe ori, iar un audit_id aleator ar
transforma fiecare relivrare într-un caz nou.
"""

from __future__ import annotations

import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

COLLECTION = "audits"

AuditStatus = Literal[
    "queued",
    "triaging",
    "rejected",
    "extracting",
    "verifying",
    "reconciling",
    "drafting",
    "delivering",
    "done",
    "failed",
]

STATUS_ORDER: tuple[AuditStatus, ...] = (
    "queued",
    "triaging",
    "extracting",
    "verifying",
    "reconciling",
    "drafting",
    "delivering",
    "done",
)


class StateError(RuntimeError):
    """Operație imposibilă pe starea unui audit."""


def audit_id_for(source_uri: str, content_hash: str | None = None) -> str:
    """Identificator determinist pentru un fișier sursă.

    Fără `content_hash`, identitatea e calea din bucket: relivrările aceluiași
    eveniment cad pe același document. Cu `content_hash`, o listă nouă încărcată
    peste aceeași cale primește un audit nou, ceea ce e comportamentul dorit
    pentru rulările lunare care refolosesc un nume de fișier.
    """
    material = source_uri if content_hash is None else f"{source_uri}\n{content_hash}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"aud-{digest[:16]}"


@dataclass
class StepLogEntry:
    """O etapă a pipeline-ului, cu ce a produs și cât a durat."""

    step: str
    status: Literal["started", "ok", "failed"]
    at: float = field(default_factory=time.time)
    duration_s: float | None = None
    detail: str | None = None
    output: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditRecord:
    """Documentul complet al unui audit."""

    audit_id: str
    source_uri: str
    status: AuditStatus = "queued"
    association_ref: str | None = None
    period: str | None = None
    document_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    step_log: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    coverage_report: dict[str, Any] | None = None
    artifact_uris: list[str] = field(default_factory=list)
    triage: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditRecord:
        known = {key: data.get(key) for key in cls.__dataclass_fields__}
        known["audit_id"] = data["audit_id"]
        known["source_uri"] = data["source_uri"]
        for key, default in (
            ("step_log", []),
            ("findings", []),
            ("artifact_uris", []),
        ):
            if known.get(key) is None:
                known[key] = list(default)
        if known.get("status") is None:
            known["status"] = "queued"
        for key in ("created_at", "updated_at"):
            if known.get(key) is None:
                known[key] = time.time()
        return cls(**known)  # type: ignore[arg-type]


class AuditStore(ABC):
    """Contractul de persistență. Implementat de Firestore și de un dublu în memorie."""

    @abstractmethod
    def get(self, audit_id: str) -> AuditRecord | None: ...

    @abstractmethod
    def put(self, record: AuditRecord) -> None: ...

    @abstractmethod
    def history(self, association_ref: str) -> list[AuditRecord]: ...

    @abstractmethod
    def list_recent(self, limit: int = 50) -> list[AuditRecord]:
        """Cele mai recente audituri, cel mai nou primul.

        Face parte din contract, nu doar din implementarea Firestore: pagina de
        inspectie depinde de ea, iar un dublu de test fara ea ar da o pagina
        goala in loc de un esec vizibil.
        """
        ...

    # ---- operații compuse, comune tuturor implementărilor ----

    def open_audit(
        self, source_uri: str, content_hash: str | None = None
    ) -> AuditRecord:
        """Deschide sau recuperează auditul unui fișier. Idempotent."""
        audit_id = audit_id_for(source_uri, content_hash)
        existing = self.get(audit_id)
        if existing is not None:
            return existing
        record = AuditRecord(audit_id=audit_id, source_uri=source_uri)
        self.put(record)
        return record

    def _load(self, audit_id: str) -> AuditRecord:
        record = self.get(audit_id)
        if record is None:
            raise StateError(f"audit inexistent: {audit_id}")
        return record

    def set_status(
        self, audit_id: str, status: AuditStatus, detail: str | None = None
    ) -> AuditRecord:
        record = self._load(audit_id)
        record.status = status
        record.updated_at = time.time()
        if detail:
            record.error = detail if status == "failed" else record.error
        self.put(record)
        return record

    def start_step(self, audit_id: str, step: str) -> float:
        record = self._load(audit_id)
        record.step_log.append(
            StepLogEntry(step=step, status="started").to_dict()
        )
        record.updated_at = time.time()
        self.put(record)
        return time.time()

    def finish_step(
        self,
        audit_id: str,
        step: str,
        started: float,
        output: dict[str, Any] | None = None,
        detail: str | None = None,
    ) -> AuditRecord:
        record = self._load(audit_id)
        record.step_log.append(
            StepLogEntry(
                step=step,
                status="ok",
                duration_s=round(time.time() - started, 3),
                detail=detail,
                output=output,
            ).to_dict()
        )
        record.updated_at = time.time()
        self.put(record)
        return record

    def fail_step(
        self, audit_id: str, step: str, started: float, error: str
    ) -> AuditRecord:
        record = self._load(audit_id)
        record.step_log.append(
            StepLogEntry(
                step=step,
                status="failed",
                duration_s=round(time.time() - started, 3),
                detail=error,
            ).to_dict()
        )
        record.status = "failed"
        record.error = error
        record.updated_at = time.time()
        self.put(record)
        return record

    def set_document_identity(
        self,
        audit_id: str,
        association_ref: str,
        period: str,
        document_id: str,
    ) -> AuditRecord:
        """Leagă auditul de asociație, ca rulările lunare să formeze un istoric."""
        record = self._load(audit_id)
        record.association_ref = association_ref
        record.period = period
        record.document_id = document_id
        record.updated_at = time.time()
        self.put(record)
        return record

    def save_results(
        self,
        audit_id: str,
        findings: list[dict[str, Any]],
        coverage_report: dict[str, Any],
    ) -> AuditRecord:
        record = self._load(audit_id)
        record.findings = findings
        record.coverage_report = coverage_report
        record.updated_at = time.time()
        self.put(record)
        return record

    def set_triage(self, audit_id: str, triage: dict[str, Any]) -> AuditRecord:
        """Consemneaza decizia gate-ului de intrare."""
        record = self._load(audit_id)
        record.triage = triage
        record.updated_at = time.time()
        self.put(record)
        return record

    def set_delivery(self, audit_id: str, delivery: dict[str, Any]) -> AuditRecord:
        """Consemneaza rezultatul livrarii. Nu schimba starea auditului.

        Un email netrimis nu invalideaza un audit reusit: artefactele sunt deja
        in GCS si constatarile in Firestore.
        """
        record = self._load(audit_id)
        record.delivery = delivery
        record.updated_at = time.time()
        self.put(record)
        return record

    def add_artifacts(self, audit_id: str, uris: list[str]) -> AuditRecord:
        record = self._load(audit_id)
        for uri in uris:
            if uri not in record.artifact_uris:
                record.artifact_uris.append(uri)
        record.updated_at = time.time()
        self.put(record)
        return record


class InMemoryAuditStore(AuditStore):
    """Dublu pentru teste și pentru rulări locale. Nicio dependență de rețea."""

    def __init__(self) -> None:
        self._records: dict[str, AuditRecord] = {}

    def get(self, audit_id: str) -> AuditRecord | None:
        record = self._records.get(audit_id)
        return AuditRecord.from_dict(record.to_dict()) if record else None

    def put(self, record: AuditRecord) -> None:
        self._records[record.audit_id] = AuditRecord.from_dict(record.to_dict())

    def history(self, association_ref: str) -> list[AuditRecord]:
        matching = [
            record
            for record in self._records.values()
            if record.association_ref == association_ref
        ]
        return sorted(
            matching, key=lambda record: (record.period or "", record.created_at)
        )

    def list_recent(self, limit: int = 50) -> list[AuditRecord]:
        ordered = sorted(
            self._records.values(), key=lambda record: record.created_at, reverse=True
        )
        return [AuditRecord.from_dict(r.to_dict()) for r in ordered[:limit]]


class FirestoreAuditStore(AuditStore):
    """Persistență în Firestore, colecția `audits`."""

    def __init__(self, project: str | None = None, collection: str = COLLECTION) -> None:
        from google.cloud import firestore  # import local: nu-l cer offline

        self._client = firestore.Client(project=project)
        self._collection = collection

    def get(self, audit_id: str) -> AuditRecord | None:
        snapshot = self._client.collection(self._collection).document(audit_id).get()
        if not snapshot.exists:
            return None
        return AuditRecord.from_dict(snapshot.to_dict() or {})

    def put(self, record: AuditRecord) -> None:
        self._client.collection(self._collection).document(record.audit_id).set(
            record.to_dict()
        )

    def history(self, association_ref: str) -> list[AuditRecord]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self._client.collection(self._collection).where(
            filter=FieldFilter("association_ref", "==", association_ref)
        )
        records = [
            AuditRecord.from_dict(doc.to_dict() or {}) for doc in query.stream()
        ]
        return sorted(records, key=lambda r: (r.period or "", r.created_at))

    def list_recent(self, limit: int = 50) -> list[AuditRecord]:
        from google.cloud import firestore

        query = (
            self._client.collection(self._collection)
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [AuditRecord.from_dict(doc.to_dict() or {}) for doc in query.stream()]

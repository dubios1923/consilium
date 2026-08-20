"""Orchestratorul: ADK, patru sub-agenți specializați, o singură trecere.

Fiecare etapă e un sub-agent cu o singură responsabilitate, nu un prompt care
face tot. Din cei patru, doar Extractor și Drafter ating un model; Integrity și
Reconciler rulează cod determinist. Împachetarea lor ca `LlmAgent` ar anula exact
garanția pentru care există: un audit reproductibil, explicabil rând cu rând.
Sunt sub-agenți ADK ca să intre în aceeași secvență, în același log de evenimente
și în aceeași stare de sesiune, nu ca să pară inteligenți.

Fiecare tranziție scrie în Firestore înainte și după: dacă pipeline-ul moare,
`step_log` arată unde.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types
from pydantic import ConfigDict

from consilium import delivery as delivery_module
from consilium import drafter as drafter_module
from consilium.config import Config
from consilium.extractor import extract, resolve_integrity
from consilium.integrity import (
    IntegrityIssue,
    IntegrityReport,
    Resolution,
    check_integrity,
)
from consilium.reconciler import audit as run_audit_rules
from consilium.schema import PaymentList
from consilium.state import AuditStore
from consilium.triage import triage as run_triage

# `SequentialAgent` nu se oprește la `ctx.end_invocation`: verifică doar
# `should_pause_invocation`. Ca să oprim lanțul (fie pentru că documentul a fost
# respins, fie pentru că o etapă a eșuat) punem un steag în starea sesiunii, pe
# care fiecare etapă îl verifică înainte să pornească.
STOP_FLAG = "pipeline_stopped"

STEP_TRIAGE = "triage"
STEP_EXTRACT = "extract"
STEP_VERIFY = "verify"
STEP_RECONCILE = "reconcile"
STEP_DRAFT = "draft"
STEP_DELIVER = "deliver"


@dataclass
class PipelineContext:
    """Ce au nevoie toți sub-agenții: persistență, praguri, client, destinație."""

    store: AuditStore
    config: Config
    client: Any = None
    artifact_destination: str = "artifacts"


# --------------------------------------------------------------------------
# Serializarea rezoluției R0 prin starea de sesiune
#
# Starea ADK trebuie să rămână serializabilă JSON, deci Resolution se
# desface aici, în orchestrator, fără să atingem consilium/integrity.py.
# --------------------------------------------------------------------------


def artifact_destination_for(base: str, audit_id: str) -> str:
    """Directorul de artefacte al unui audit.

    Fiecare audit primește propriul director. Fără el, `findings.csv` și
    `audit_report.json` ale unui audit le suprascriu pe ale precedentului:
    numai scrisoarea poartă audit_id în nume.
    """
    if base.startswith("gs://"):
        return f"{base.rstrip('/')}/{audit_id}"
    return str(Path(base) / audit_id)


def _as_of(state: dict[str, Any]) -> date | None:
    """Data de referință pentru termenele procedurale, luată din sesiune."""
    raw = state.get("as_of")
    return date.fromisoformat(raw) if raw else None


def resolution_to_state(resolution: Resolution) -> dict[str, Any]:
    return {
        "unauditable_apartments": sorted(resolution.unauditable_apartments),
        "unauditable_categories": sorted(resolution.unauditable_categories),
        "low_confidence_fields": list(resolution.low_confidence_fields),
        "reread_calls": resolution.reread_calls,
        "confirmed_inconsistencies": [
            {
                "rule_id": issue.rule_id,
                "scope": issue.scope,
                "expected": issue.expected,
                "found": issue.found,
                "delta": issue.delta,
                "message": issue.message,
                "apartment_no": issue.apartment_no,
                "category": issue.category,
            }
            for issue in resolution.confirmed_inconsistencies
        ],
    }


def resolution_from_state(data: dict[str, Any]) -> Resolution:
    return Resolution(
        report=IntegrityReport(),
        confirmed_inconsistencies=[
            IntegrityIssue(**issue) for issue in data["confirmed_inconsistencies"]
        ],
        unauditable_apartments=set(data["unauditable_apartments"]),
        unauditable_categories=set(data["unauditable_categories"]),
        low_confidence_fields=list(data["low_confidence_fields"]),
        reread_calls=data["reread_calls"],
    )


# --------------------------------------------------------------------------
# Sub-agenți
# --------------------------------------------------------------------------


class _Stage(BaseAgent):
    """Bază comună: marchează etapa în Firestore, rulează, raportează."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    pipeline: PipelineContext
    step: str
    status: str

    def _note(self, text: str, state: dict[str, Any] | None = None) -> Event:
        return Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part.from_text(text=text)]),
            actions=EventActions(state_delta=state or {}),
        )

    def _stop(self, ctx: InvocationContext) -> None:
        """Oprește etapele următoare. Mutăm direct starea sesiunii, ca steagul să
        fie vizibil imediat, nu după ce runner-ul procesează evenimentul."""
        ctx.session.state[STOP_FLAG] = True
        ctx.end_invocation = True

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        if ctx.session.state.get(STOP_FLAG):
            return

        audit_id = ctx.session.state["audit_id"]
        store = self.pipeline.store
        store.set_status(audit_id, self.status)  # type: ignore[arg-type]
        started = store.start_step(audit_id, self.step)
        try:
            summary, delta = self.execute(ctx)
        except Exception as error:  # noqa: BLE001 - eșecul trebuie persistat
            store.fail_step(
                audit_id, self.step, started, f"{type(error).__name__}: {error}"
            )
            self._stop(ctx)
            yield self._note(f"{self.step}: eșec, {error}", {STOP_FLAG: True})
            return

        store.finish_step(audit_id, self.step, started, output=delta.get("_log"))
        delta.pop("_log", None)
        if delta.pop("_stop", False):
            # Oprire curată, nu eșec: documentul a fost rutat corect în afară.
            self._stop(ctx)
            delta[STOP_FLAG] = True
        yield self._note(summary, delta)

    def execute(self, ctx: InvocationContext) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError


class TriageAgent(_Stage):
    """Gate de intrare. Oprește documentele care nu sunt liste de plată.

    Eșuează deschis: dacă triajul nu poate decide, pipeline-ul continuă. Un gate
    de optimizare nu are dreptul să blocheze un audit valid.
    """

    def execute(self, ctx: InvocationContext) -> tuple[str, dict[str, Any]]:
        state = ctx.session.state
        audit_id = state["audit_id"]
        outcome = run_triage(
            state["pdf_path"], self.pipeline.config, self.pipeline.client
        )
        self.pipeline.store.set_triage(audit_id, outcome.to_dict())

        if outcome.should_continue:
            summary = (
                f"Triaj: {outcome.status}"
                + (f" ({outcome.document_type})" if outcome.document_type else "")
                + (f". {outcome.reason}" if outcome.reason else "")
            )
            return summary, {
                "triage_status": outcome.status,
                "_log": {
                    "status": outcome.status,
                    "document_type": outcome.document_type,
                    "confidence": outcome.confidence,
                    "error": outcome.error,
                },
            }

        self.pipeline.store.set_status(audit_id, "rejected")
        summary = (
            f"Document respins: {outcome.document_type or 'tip necunoscut'}. "
            f"{outcome.reason}"
        )
        return summary, {
            "triage_status": outcome.status,
            "_stop": True,
            "_log": {
                "status": outcome.status,
                "document_type": outcome.document_type,
                "confidence": outcome.confidence,
                "reason": outcome.reason,
            },
        }


class ExtractorAgent(_Stage):
    """Transcrie PDF-ul în PaymentList. Singurul pas care citește documentul."""

    def execute(self, ctx: InvocationContext) -> tuple[str, dict[str, Any]]:
        state = ctx.session.state
        payment = extract(state["pdf_path"], client=self.pipeline.client)
        self.pipeline.store.set_document_identity(
            state["audit_id"],
            payment.association_ref,
            payment.period,
            payment.document_id,
        )
        summary = (
            f"Extras {len(payment.apartment_lines)} apartamente și "
            f"{len(payment.expense_lines)} poziții de cheltuială din "
            f"{payment.document_id}."
        )
        return summary, {
            "payment_json": payment.model_dump_json(),
            "_log": {
                "apartment_lines": len(payment.apartment_lines),
                "expense_lines": len(payment.expense_lines),
                "low_confidence_fields": len(
                    payment.extraction_confidence.low_confidence_fields
                ),
            },
        }


class IntegrityAgent(_Stage):
    """R0 și recitirea țintită. Separă erorile de citire de erorile documentului."""

    def execute(self, ctx: InvocationContext) -> tuple[str, dict[str, Any]]:
        state = ctx.session.state
        payment = PaymentList.model_validate_json(state["payment_json"])
        report = check_integrity(payment, self.pipeline.config)
        resolution = resolve_integrity(
            payment,
            report,
            self.pipeline.config,
            state["pdf_path"],
            self.pipeline.client,
        )
        summary = (
            f"R0: {len(report.issues)} incoerențe, {resolution.reread_calls} "
            f"recitiri, {len(resolution.unauditable_apartments)} apartamente "
            f"neauditabile."
        )
        return summary, {
            "payment_json": payment.model_dump_json(),
            "resolution": resolution_to_state(resolution),
            "_log": {
                "r0_issues": len(report.issues),
                "reread_calls": resolution.reread_calls,
                "unauditable_apartments": sorted(resolution.unauditable_apartments),
            },
        }


class ReconcilerAgent(_Stage):
    """R1-R6. Determinist, fără rețea, fără model."""

    def execute(self, ctx: InvocationContext) -> tuple[str, dict[str, Any]]:
        state = ctx.session.state
        payment = PaymentList.model_validate_json(state["payment_json"])
        resolution = resolution_from_state(state["resolution"])
        result = run_audit_rules(
            payment, self.pipeline.config, resolution, _as_of(state)
        )

        findings = [
            {
                column: getattr(finding, column)
                for column in drafter_module.FINDINGS_CSV_COLUMNS
            }
            for finding in result.findings
        ]
        coverage = {
            "summary": result.coverage.summary(),
            "rules": [
                {
                    "rule_id": rule.rule_id,
                    "status": rule.status,
                    "checked": rule.checked,
                    "total": rule.total,
                    "reason": rule.reason,
                }
                for rule in result.coverage.rules
            ],
            "expense_lines_verified": result.coverage.expense_lines_verified,
            "expense_lines_unverified": [
                {"category": category, "reason": reason}
                for category, reason in result.coverage.expense_lines_unverified
            ],
            "documents_to_request": result.coverage.documents_to_request,
        }
        self.pipeline.store.save_results(state["audit_id"], findings, coverage)

        summary = (
            f"{len(result.findings)} constatări, "
            f"{result.total_amount_involved:.2f} lei implicați; "
            f"{result.coverage.summary()}."
        )
        return summary, {
            "findings_count": len(result.findings),
            "_log": {
                "findings": [finding.rule_id for finding in result.findings],
                "total_amount_involved": result.total_amount_involved,
            },
        }


class DrafterAgent(_Stage):
    """Redactează cererea și scrie artefactele.

    Nu e un `LlmAgent`: generarea trece printr-o buclă generează-verifică-reia
    care respinge orice scrisoare ce citează o sumă neprovenită dintr-o
    constatare. Bucla nu se poate exprima ca un simplu prompt.
    """

    def execute(self, ctx: InvocationContext) -> tuple[str, dict[str, Any]]:
        state = ctx.session.state
        audit_id = state["audit_id"]
        payment = PaymentList.model_validate_json(state["payment_json"])
        resolution = resolution_from_state(state["resolution"])
        result = run_audit_rules(
            payment, self.pipeline.config, resolution, _as_of(state)
        )

        draft = drafter_module.draft_letter(
            payment,
            result,
            self.pipeline.client,
            self.pipeline.config,
            audit_id=audit_id,
        )
        destination = artifact_destination_for(
            self.pipeline.artifact_destination, audit_id
        )
        uris = drafter_module.write_artifacts(
            destination, audit_id, payment, result, draft
        )
        self.pipeline.store.add_artifacts(audit_id, uris)

        # Copie locală a scrisorii, pentru pasul de livrare: `write_artifacts`
        # încarcă în GCS dintr-un director temporar care apoi dispare.
        local_dir = Path(tempfile.gettempdir()) / "consilium" / audit_id
        local_dir.mkdir(parents=True, exist_ok=True)
        local_pdf = drafter_module.render_letter_pdf(
            draft, local_dir / f"cerere_documente_{audit_id}.pdf"
        )

        return f"Scrisoare redactată; {len(uris)} artefacte scrise.", {
            "artifact_uris": uris,
            "letter_local_path": str(local_pdf),
            "requested_documents": list(draft.requested_documents),
            "_log": {"artifact_uris": uris},
        }


class DeliveryAgent(_Stage):
    """Trimite scrisoarea pe email. Pas opțional care nu poate rupe auditul.

    Nu ridică niciodată: dacă livrarea eșuează, motivul ajunge în Firestore sub
    `delivery`, iar auditul rămâne reușit. Artefactele sunt deja în GCS, deci
    scrisoarea nu se pierde, doar nu ajunge singură la destinatar.
    """

    def execute(self, ctx: InvocationContext) -> tuple[str, dict[str, Any]]:
        state = ctx.session.state
        audit_id = state["audit_id"]
        config = delivery_module.DeliveryConfig.from_env()

        if config is None:
            outcome = delivery_module.DeliveryOutcome(
                status="skipped", error="livrare neconfigurată"
            )
        else:
            outcome = self._send(state, audit_id, config)

        self.pipeline.store.set_delivery(audit_id, outcome.to_dict())
        summary = f"Livrare: {outcome.status}"
        if outcome.recipient:
            summary += f" către {outcome.recipient}"
        if outcome.error:
            summary += f". {outcome.error}"
        return summary, {
            "delivery_status": outcome.status,
            "_log": {"delivery": outcome.status, "error": outcome.error},
        }

    def _send(
        self,
        state: dict[str, Any],
        audit_id: str,
        config: delivery_module.DeliveryConfig,
    ) -> delivery_module.DeliveryOutcome:
        try:
            local_pdf = state.get("letter_local_path")
            if not local_pdf:
                return delivery_module.DeliveryOutcome(
                    status="failed",
                    recipient=config.recipient,
                    error="calea locală a scrisorii lipsește din starea sesiunii",
                )
            payment = PaymentList.model_validate_json(state["payment_json"])
            resolution = resolution_from_state(state["resolution"])
            result = run_audit_rules(
                payment, self.pipeline.config, resolution, _as_of(state)
            )
            return delivery_module.deliver(
                payment,
                result,
                state.get("requested_documents", []),
                local_pdf,
                config,
                audit_id=audit_id,
            )
        except Exception as error:  # noqa: BLE001 - livrarea nu rupe auditul
            return delivery_module.DeliveryOutcome(
                status="failed",
                recipient=config.recipient,
                error=f"{type(error).__name__}: {error}",
            )


# --------------------------------------------------------------------------
# Asamblare
# --------------------------------------------------------------------------


def build_pipeline(pipeline: PipelineContext) -> SequentialAgent:
    """Construiește secvența celor patru sub-agenți."""
    return SequentialAgent(
        name="consilium_pipeline",
        description=(
            "Auditează o listă de plată: transcriere, verificarea transcrierii, "
            "reconciliere determinist și redactarea cererii de documente."
        ),
        sub_agents=[
            TriageAgent(
                name="triage",
                description="Decide dacă documentul merită pipeline-ul complet.",
                pipeline=pipeline,
                step=STEP_TRIAGE,
                status="triaging",
            ),
            ExtractorAgent(
                name="extractor",
                description="Transcrie PDF-ul în structura validată.",
                pipeline=pipeline,
                step=STEP_EXTRACT,
                status="extracting",
            ),
            IntegrityAgent(
                name="integrity",
                description="R0 și recitirea țintită a zonelor suspecte.",
                pipeline=pipeline,
                step=STEP_VERIFY,
                status="verifying",
            ),
            ReconcilerAgent(
                name="reconciler",
                description="Regulile R1-R6, determinist.",
                pipeline=pipeline,
                step=STEP_RECONCILE,
                status="reconciling",
            ),
            DrafterAgent(
                name="drafter",
                description="Cererea formală de documente și artefactele.",
                pipeline=pipeline,
                step=STEP_DRAFT,
                status="drafting",
            ),
            DeliveryAgent(
                name="delivery",
                description="Trimite scrisoarea pe email, dacă e configurat.",
                pipeline=pipeline,
                step=STEP_DELIVER,
                status="delivering",
            ),
        ],
    )


async def run_audit(
    pdf_path: str | Path,
    source_uri: str,
    pipeline: PipelineContext,
    content_hash: str | None = None,
    as_of: date | None = None,
) -> str:
    """Rulează pipeline-ul complet pentru un fișier. Întoarce audit_id."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    record = pipeline.store.open_audit(source_uri, content_hash)
    audit_id = record.audit_id

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="consilium",
        user_id="pipeline",
        state={
            "audit_id": audit_id,
            "source_uri": source_uri,
            "pdf_path": str(pdf_path),
            "started_at": time.time(),
            "as_of": as_of.isoformat() if as_of else None,
        },
    )
    runner = Runner(
        app_name="consilium",
        agent=build_pipeline(pipeline),
        session_service=session_service,
    )
    async for _ in runner.run_async(
        user_id="pipeline",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text=f"Auditează {source_uri}")]
        ),
    ):
        pass

    final = pipeline.store.get(audit_id)
    if final and final.status not in ("failed", "rejected"):
        pipeline.store.set_status(audit_id, "done")
    return audit_id

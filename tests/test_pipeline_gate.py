"""Gate-ul în contextul pipeline-ului complet. Offline, fără niciun apel real.

Testul care contează: un document respins nu trebuie doar marcat respins, ci să
nu ajungă niciodată la extractor. Dacă îl atinge, gate-ul nu a economisit nimic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from consilium.config import Config
from consilium.drafter import LetterDraft
from consilium.pipeline import PipelineContext, run_audit
from consilium.schema import PaymentList
from consilium.state import InMemoryAuditStore
from consilium.triage import TriageOutcome
from tests.conftest import CONFIG_DATA

SOURCE = "gs://intake/liste/document.pdf"
PDF = "samples/synthetic/sample_clean.pdf"

FULL_CONFIG = {
    **CONFIG_DATA,
    "triage": {
        "enabled": True,
        "model": "model-de-test",
        "dpi": 110,
        "max_output_tokens": 400,
    },
}


class ExtractorSpy:
    """Numără câte documente au ajuns la extracție."""

    def __init__(self, payment: PaymentList) -> None:
        self.payment = payment
        self.calls = 0

    def __call__(self, pdf_path, client=None):
        self.calls += 1
        return self.payment


@pytest.fixture
def payment() -> PaymentList:
    return PaymentList.model_validate_json(
        Path("samples/extracted/sample_clean.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def store() -> InMemoryAuditStore:
    return InMemoryAuditStore()


def wire(monkeypatch, payment, triage_outcome: TriageOutcome) -> ExtractorSpy:
    """Înlocuiește tot ce ar ieși în rețea. Restul pipeline-ului rulează real."""
    import consilium.pipeline as pipeline_module

    spy = ExtractorSpy(payment)
    monkeypatch.setattr(pipeline_module, "run_triage", lambda *a, **k: triage_outcome)
    monkeypatch.setattr(pipeline_module, "extract", spy)
    monkeypatch.setattr(
        pipeline_module.drafter_module,
        "draft_letter",
        lambda *a, **k: LetterDraft(
            subject="s", opening="o", paragraphs=[], coverage_paragraph="c",
            requested_documents=["d"], deadline_paragraph="t", closing="î",
        ),
    )
    monkeypatch.setattr(
        pipeline_module.drafter_module, "write_artifacts", lambda *a, **k: []
    )
    monkeypatch.setattr(
        pipeline_module.drafter_module,
        "render_letter_pdf",
        lambda draft, target: Path(target),
    )
    return spy


def run(store, monkeypatch, payment, outcome):
    spy = wire(monkeypatch, payment, outcome)
    context = PipelineContext(store=store, config=Config(FULL_CONFIG), client=None)
    audit_id = asyncio.run(run_audit(PDF, SOURCE, context))
    return audit_id, spy


def steps(record) -> list[str]:
    return [entry["step"] for entry in record.step_log if entry["status"] != "started"]


# --------------------------------------------------------------------------
# Respingere
# --------------------------------------------------------------------------


def test_rejected_document_never_reaches_the_extractor(store, monkeypatch, payment):
    outcome = TriageOutcome(
        is_payment_list=False,
        status="rejected",
        document_type="hotărâre a adunării generale",
        reason="nu conține tabel de repartizare",
    )
    audit_id, spy = run(store, monkeypatch, payment, outcome)
    assert spy.calls == 0, "gate-ul nu a oprit nimic"
    record = store.get(audit_id)
    assert record.status == "rejected"
    assert steps(record) == ["triage"]


def test_rejection_reason_is_persisted(store, monkeypatch, payment):
    outcome = TriageOutcome(
        is_payment_list=False,
        status="rejected",
        document_type="factură furnizor",
        reason="este o factură de la furnizorul de energie",
    )
    audit_id, _ = run(store, monkeypatch, payment, outcome)
    record = store.get(audit_id)
    assert record.triage["document_type"] == "factură furnizor"
    assert "factură" in record.triage["reason"]
    assert record.error is None, "respingerea nu e o eroare"


def test_rejected_audit_produces_no_findings_or_artifacts(store, monkeypatch, payment):
    outcome = TriageOutcome(is_payment_list=False, status="rejected", reason="x")
    audit_id, _ = run(store, monkeypatch, payment, outcome)
    record = store.get(audit_id)
    assert record.findings == []
    assert record.artifact_uris == []
    assert record.coverage_report is None


# --------------------------------------------------------------------------
# Fail open
# --------------------------------------------------------------------------


def test_unavailable_triage_runs_the_full_pipeline(store, monkeypatch, payment):
    outcome = TriageOutcome(
        is_payment_list=True,
        status="unavailable",
        reason="triajul nu a putut decide",
        error="RuntimeError: model căzut",
    )
    audit_id, spy = run(store, monkeypatch, payment, outcome)
    assert spy.calls == 1, "un gate căzut nu are voie să blocheze auditul"
    record = store.get(audit_id)
    assert record.status == "done"
    assert record.findings == []
    assert record.triage["status"] == "unavailable"


def test_accepted_document_follows_the_unchanged_path(store, monkeypatch, payment):
    outcome = TriageOutcome(
        is_payment_list=True, status="accepted", document_type="listă de plată"
    )
    audit_id, spy = run(store, monkeypatch, payment, outcome)
    assert spy.calls == 1
    record = store.get(audit_id)
    assert record.status == "done"
    assert steps(record) == ["triage", "extract", "verify", "reconcile", "draft", "deliver"]


def test_triage_is_the_first_step(store, monkeypatch, payment):
    outcome = TriageOutcome(is_payment_list=True, status="accepted")
    audit_id, _ = run(store, monkeypatch, payment, outcome)
    assert steps(store.get(audit_id))[0] == "triage"


def test_delivery_stays_skipped_without_configuration(store, monkeypatch, payment):
    monkeypatch.delenv("CONSILIUM_DELIVERY_TO", raising=False)
    monkeypatch.delenv("CONSILIUM_DELIVERY_API_KEY", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    outcome = TriageOutcome(is_payment_list=True, status="accepted")
    audit_id, _ = run(store, monkeypatch, payment, outcome)
    assert store.get(audit_id).delivery["status"] == "skipped"


def test_a_failing_stage_stops_the_ones_after_it(store, monkeypatch, payment):
    """`SequentialAgent` nu se oprește singur la eșec: steagul trebuie să o facă."""
    import consilium.pipeline as pipeline_module

    outcome = TriageOutcome(is_payment_list=True, status="accepted")
    wire(monkeypatch, payment, outcome)

    def exploding(pdf_path, client=None):
        raise RuntimeError("extracția a căzut")

    monkeypatch.setattr(pipeline_module, "extract", exploding)
    context = PipelineContext(store=store, config=Config(FULL_CONFIG), client=None)
    audit_id = asyncio.run(run_audit(PDF, SOURCE, context))

    record = store.get(audit_id)
    assert record.status == "failed"
    assert "extracția a căzut" in record.error
    assert steps(record) == ["triage", "extract"], "etapele următoare au rulat"
    assert record.findings == []

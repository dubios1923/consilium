"""Pagina de inspecție. Offline: Firestore înlocuit cu dublul în memorie.

Invarianta care contează: pagina e read-only. Nu are nicio rută care să
pornească, să reia sau să modifice un audit.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from consilium import dashboard
from consilium.state import InMemoryAuditStore

pytest.importorskip("httpx")


@pytest.fixture
def store(monkeypatch) -> InMemoryAuditStore:
    memory = InMemoryAuditStore()
    monkeypatch.setattr(dashboard, "store", lambda: memory)
    return memory


@pytest.fixture
def client(store) -> TestClient:
    return TestClient(dashboard.app)


def seed_done(store: InMemoryAuditStore) -> str:
    record = store.open_audit("gs://intake/liste/zefir12.pdf")
    store.set_document_identity(
        record.audit_id, "Asociația „Zefir 12”", "2025-11", "LP-2025-11"
    )
    store.set_triage(
        record.audit_id,
        {"status": "accepted", "document_type": "listă de plată",
         "confidence": "high", "reason": "conține tabel", "model": "m", "error": None},
    )
    started = store.start_step(record.audit_id, "extract")
    store.finish_step(record.audit_id, "extract", started)
    store.save_results(
        record.audit_id,
        [
            {"rule_id": "R4", "severity": "high", "apartment_no": "17",
             "category": None, "amount_involved": 73.95,
             "legal_reference": "art. 77 alin. (2)", "message": "penalizare peste plafon"},
        ],
        {"summary": "8 din 8 poziții de cheltuială verificabile",
         "rules": [{"rule_id": "R4", "status": "verificat", "checked": 28,
                    "total": 28, "reason": None}],
         "expense_lines_unverified": [],
         "documents_to_request": ["Facturile furnizorilor."]},
    )
    store.add_artifacts(
        record.audit_id, ["gs://intake/output/x/cerere.pdf", "gs://intake/output/x/f.csv"]
    )
    store.set_delivery(
        record.audit_id,
        {"status": "delivered", "recipient": "a@b.ro", "message_id": "msg-1",
         "error": None, "at": 1.0},
    )
    store.set_status(record.audit_id, "done")
    return record.audit_id


# --------------------------------------------------------------------------
# Read-only
# --------------------------------------------------------------------------


def test_no_mutating_routes_exist():
    """Un refresh într-un demo nu are voie să pornească o procesare."""
    methods = set()
    for route in dashboard.app.routes:
        methods |= set(getattr(route, "methods", set()) or set())
    assert methods <= {"GET", "HEAD"}, f"rute care pot modifica: {methods}"


def test_health_endpoint(client):
    assert client.get("/healthz").json() == {"status": "ok"}


# --------------------------------------------------------------------------
# Lista
# --------------------------------------------------------------------------


def test_empty_state_is_explained(client):
    body = client.get("/").text
    assert "Niciun audit încă" in body


def test_index_lists_the_audit(client, store):
    audit_id = seed_done(store)
    body = client.get("/").text
    assert audit_id in body
    assert "Zefir 12" in body
    assert "finalizat" in body


def test_index_refreshes_only_while_something_runs(client, store):
    seed_done(store)
    assert "http-equiv=\"refresh\"" not in client.get("/").text
    running = store.open_audit("gs://intake/liste/altul.pdf")
    store.set_status(running.audit_id, "extracting")
    assert "http-equiv=\"refresh\"" in client.get("/").text


def test_rejected_audits_are_counted_separately(client, store):
    record = store.open_audit("gs://intake/liste/aga.pdf")
    store.set_triage(record.audit_id, {"status": "rejected",
                                       "document_type": "hotărâre AGA",
                                       "confidence": "high", "reason": "nu e listă",
                                       "model": "m", "error": None})
    store.set_status(record.audit_id, "rejected")
    body = client.get("/").text
    assert "respinse la triaj" in body
    assert "respins" in body


# --------------------------------------------------------------------------
# Detaliu
# --------------------------------------------------------------------------


def test_detail_shows_findings_coverage_and_delivery(client, store):
    audit_id = seed_done(store)
    body = client.get(f"/audit/{audit_id}").text
    assert "R4, apartamentul 17" in body
    assert "73,95 lei" in body, "sumele se afișează în format românesc"
    assert "art. 77 alin. (2)" in body
    assert "8 din 8 poziții" in body
    assert "Facturile furnizorilor." in body
    assert "a@b.ro" in body
    assert "Extragere" in body


def test_detail_of_rejected_audit_explains_the_decision(client, store):
    record = store.open_audit("gs://intake/liste/aga.pdf")
    store.set_triage(record.audit_id, {"status": "rejected",
                                       "document_type": "hotărâre AGA",
                                       "confidence": "high",
                                       "reason": "nu conține repartizare",
                                       "model": "m", "error": None})
    store.set_status(record.audit_id, "rejected")
    body = client.get(f"/audit/{record.audit_id}").text
    assert "Document respins la intrare" in body
    assert "hotărâre AGA" in body
    assert "nu conține repartizare" in body


def test_clean_document_says_so_instead_of_showing_nothing(client, store):
    record = store.open_audit("gs://intake/liste/curat.pdf")
    store.save_results(record.audit_id, [], {"summary": "8 din 8", "rules": [],
                                             "expense_lines_unverified": [],
                                             "documents_to_request": []})
    store.set_status(record.audit_id, "done")
    body = client.get(f"/audit/{record.audit_id}").text
    assert "lista de plată este consistentă" in body


def test_unknown_audit_is_404(client):
    assert client.get("/audit/aud-inexistent").status_code == 404


def test_failure_reason_is_shown(client, store):
    record = store.open_audit("gs://intake/liste/x.pdf")
    started = store.start_step(record.audit_id, "extract")
    store.fail_step(record.audit_id, "extract", started, "RuntimeError: cazut")
    body = client.get(f"/audit/{record.audit_id}").text
    assert "RuntimeError: cazut" in body


# --------------------------------------------------------------------------
# Escaping
# --------------------------------------------------------------------------


def test_record_content_is_escaped(client, store):
    record = store.open_audit("gs://intake/liste/x.pdf")
    store.set_document_identity(
        record.audit_id, "<script>alert(1)</script>", "2025-11", "LP-1"
    )
    store.set_status(record.audit_id, "done")
    body = client.get(f"/audit/{record.audit_id}").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_letter_route_404s_without_a_pdf(client, store):
    record = store.open_audit("gs://intake/liste/x.pdf")
    store.set_status(record.audit_id, "done")
    assert client.get(f"/audit/{record.audit_id}/letter").status_code == 404


# --------------------------------------------------------------------------
# Formatare
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(900.0, "900,00 lei"), (3343.87, "3.343,87 lei"), (None, "—"), ("x", "—")],
)
def test_money_formatting(value, expected):
    assert dashboard.money(value) == expected

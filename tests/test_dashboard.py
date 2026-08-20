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


def test_no_route_can_modify_an_existing_audit():
    """Un refresh nu are voie sa reporneasca o procesare, oricate ori s-ar da.

    Incarcarea unui document nou nu incalca asta: scrie un fisier in bucket, ca
    `gcloud storage cp`, si nu atinge niciun dosar existent.
    """
    for route in dashboard.app.routes:
        methods = set(getattr(route, "methods", set()) or set())
        path = getattr(route, "path", "")
        if methods <= {"GET", "HEAD"}:
            continue
        assert path == "/upload", f"ruta care poate modifica: {methods} {path}"
        assert "audit" not in path


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
    [(900.0, "900,00 lei"), (3343.87, "3.343,87 lei"), (None, ""), ("x", "")],
)
def test_money_formatting(value, expected):
    assert dashboard.money(value) == expected


# --------------------------------------------------------------------------
# Încărcarea
# --------------------------------------------------------------------------


def upload(client: TestClient, name: str, payload: bytes):
    return client.post(
        "/upload",
        files={"document": (name, payload, "application/pdf")},
        follow_redirects=False,
    )


def notice_of(response) -> str:
    """Mesajul din redirect, decodat: FastAPI il trimite percent-encoded."""
    from urllib.parse import unquote_plus

    return unquote_plus(response.headers["location"])


def test_upload_form_is_on_the_page(client):
    body = client.get("/").text
    assert 'action="/upload"' in body
    assert "Încarcă o listă de plată" in body


def test_non_pdf_is_refused(client):
    response = upload(client, "note.txt", b"nu e pdf")
    assert response.status_code == 303
    assert "PDF" in notice_of(response)


def test_file_that_only_claims_to_be_pdf_is_refused(client, monkeypatch):
    """Extensia nu e o dovada; verificam antetul fisierului."""
    response = upload(client, "fals.pdf", b"GIF89a not really")
    assert response.status_code == 303
    assert "valid" in notice_of(response)


def test_oversized_file_is_refused(client):
    big = b"%PDF-1.4" + b"0" * (dashboard.MAX_UPLOAD_BYTES + 1)
    response = upload(client, "mare.pdf", big)
    assert "prea mare" in notice_of(response)


def test_daily_limit_stops_further_uploads(client, store, monkeypatch):
    monkeypatch.setattr(dashboard, "DAILY_UPLOAD_LIMIT", 1)
    store.open_audit("gs://intake/liste/deja.pdf")
    response = upload(client, "listă.pdf", b"%PDF-1.4 continut")
    assert "Plafonul" in notice_of(response)


def test_accepted_upload_writes_to_the_bucket(client, monkeypatch):
    written = {}

    class FakeBlob:
        def upload_from_string(self, payload, content_type):
            written["payload"] = payload
            written["type"] = content_type

    class FakeBucket:
        def blob(self, path):
            written["path"] = path
            return FakeBlob()

    class FakeClient:
        def bucket(self, name):
            written["bucket"] = name
            return FakeBucket()

    import google.cloud.storage as storage_module

    monkeypatch.setattr(storage_module, "Client", lambda *a, **k: FakeClient())
    response = upload(client, "zefir 12.pdf", b"%PDF-1.4 continut")
    assert response.status_code == 303
    assert "încărcat" in notice_of(response)
    assert written["bucket"] == dashboard.BUCKET
    assert written["path"].startswith("liste/")
    assert written["path"].endswith("zefir_12.pdf")
    assert written["type"] == "application/pdf"


@pytest.mark.parametrize(
    ("raw", "ends_with"),
    [
        ("listă de plată.pdf", "listă_de_plată.pdf"),
        ("../../etc/passwd", "passwd.pdf"),
        ("", "document.pdf"),
    ],
)
def test_names_are_sanitised(raw, ends_with):
    assert dashboard.safe_name(raw).endswith(ends_with)


def test_names_are_unique_per_upload():
    first = dashboard.safe_name("x.pdf")
    assert first.split("_", 1)[0].isdigit()

"""Teste pentru starea auditului. Offline, pe dublul în memorie."""

from __future__ import annotations

import pytest

from consilium.state import (
    AuditRecord,
    InMemoryAuditStore,
    StateError,
    audit_id_for,
)

SOURCE = "gs://consilium-intake-ab7x21/liste/zefir12_2025-11.pdf"


@pytest.fixture
def store() -> InMemoryAuditStore:
    return InMemoryAuditStore()


def test_same_file_reprocessed_does_not_duplicate(store):
    """Eventarc relivrează. A doua livrare trebuie să cadă pe același document."""
    first = store.open_audit(SOURCE)
    store.set_status(first.audit_id, "extracting")
    second = store.open_audit(SOURCE)
    assert second.audit_id == first.audit_id
    assert second.status == "extracting", "starea existentă nu trebuie resetată"


def test_different_paths_get_different_audits(store):
    assert (
        store.open_audit(SOURCE).audit_id
        != store.open_audit(SOURCE + ".bak").audit_id
    )


def test_content_hash_separates_reuploads_over_the_same_path():
    assert audit_id_for(SOURCE, "hash-noiembrie") != audit_id_for(
        SOURCE, "hash-decembrie"
    )


def test_audit_id_is_deterministic():
    assert audit_id_for(SOURCE) == audit_id_for(SOURCE)


def test_step_log_records_start_and_finish(store):
    record = store.open_audit(SOURCE)
    started = store.start_step(record.audit_id, "extract")
    store.finish_step(record.audit_id, "extract", started, output={"lines": 28})
    log = store.get(record.audit_id).step_log
    assert [entry["step"] for entry in log] == ["extract", "extract"]
    assert [entry["status"] for entry in log] == ["started", "ok"]
    assert log[1]["output"] == {"lines": 28}
    assert log[1]["duration_s"] is not None


def test_failure_is_persisted_with_the_reason(store):
    record = store.open_audit(SOURCE)
    started = store.start_step(record.audit_id, "extract")
    store.fail_step(record.audit_id, "extract", started, "RuntimeError: fără rețea")
    stored = store.get(record.audit_id)
    assert stored.status == "failed"
    assert "fără rețea" in stored.error
    assert stored.step_log[-1]["status"] == "failed"


def test_monthly_runs_link_through_association_ref(store):
    for period, name in (("2025-10", "oct"), ("2025-11", "noi"), ("2025-09", "sep")):
        record = store.open_audit(f"{SOURCE}.{name}")
        store.set_document_identity(
            record.audit_id, "Asociația „Zefir 12”", period, f"LP-{period}"
        )
    history = store.history("Asociația „Zefir 12”")
    assert [record.period for record in history] == ["2025-09", "2025-10", "2025-11"]


def test_history_ignores_other_associations(store):
    first = store.open_audit(SOURCE)
    store.set_document_identity(first.audit_id, "Zefir 12", "2025-11", "LP-1")
    second = store.open_audit(SOURCE + ".alt")
    store.set_document_identity(second.audit_id, "Mimoza 4", "2025-11", "LP-2")
    assert len(store.history("Zefir 12")) == 1


def test_artifacts_are_not_duplicated_on_retry(store):
    record = store.open_audit(SOURCE)
    store.add_artifacts(record.audit_id, ["gs://b/out/a.pdf"])
    store.add_artifacts(record.audit_id, ["gs://b/out/a.pdf", "gs://b/out/b.csv"])
    assert store.get(record.audit_id).artifact_uris == [
        "gs://b/out/a.pdf",
        "gs://b/out/b.csv",
    ]


def test_operations_on_unknown_audit_fail_loudly(store):
    with pytest.raises(StateError, match="inexistent"):
        store.set_status("aud-inexistent", "done")


def test_record_survives_a_dict_round_trip(store):
    record = store.open_audit(SOURCE)
    store.set_document_identity(record.audit_id, "Zefir 12", "2025-11", "LP-1")
    store.save_results(record.audit_id, [{"rule_id": "R1"}], {"summary": "8 din 8"})
    stored = store.get(record.audit_id)
    assert AuditRecord.from_dict(stored.to_dict()) == stored

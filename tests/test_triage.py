"""Gate-ul de intrare. Offline: modelul de triaj e mock-uit.

Invarianta centrală nu e că gate-ul respinge corect, ci că nu poate bloca un
audit valid. Un filtru de cost care oprește documente bune costă mai mult decât
economisește.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from consilium.config import Config, ConfigError
from consilium.triage import TriageOutcome, TriageVerdict, triage
from tests.conftest import CONFIG_DATA

TRIAGE_CONFIG = {
    **CONFIG_DATA,
    "triage": {
        "enabled": True,
        "model": "model-de-test",
        "dpi": 110,
        "max_output_tokens": 400,
    },
}


class FakeClient:
    """Client fals: întoarce verdictul cerut, sau explodează."""

    def __init__(self, verdict: TriageVerdict | None = None, error: Exception | None = None):
        self.error = error
        self.verdict = verdict
        self.calls = 0
        self.models = self

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error

        class Response:
            parsed = self.verdict
            text = None

        return Response()


@pytest.fixture
def config() -> Config:
    return Config(TRIAGE_CONFIG)


@pytest.fixture
def pdf() -> Path:
    return Path("samples/synthetic/sample_clean.pdf")


def verdict(**kwargs) -> TriageVerdict:
    base = {
        "is_payment_list": True,
        "document_type": "listă de plată",
        "confidence": "high",
        "reason": "conține tabel de cheltuieli și repartizare pe apartamente",
    }
    base.update(kwargs)
    return TriageVerdict(**base)


# --------------------------------------------------------------------------
# Acceptare și respingere
# --------------------------------------------------------------------------


def test_payment_list_is_accepted(config, pdf):
    outcome = triage(pdf, config, FakeClient(verdict()))
    assert outcome.status == "accepted"
    assert outcome.is_payment_list
    assert outcome.should_continue


def test_other_document_is_rejected(config, pdf):
    outcome = triage(
        pdf,
        config,
        FakeClient(
            verdict(
                is_payment_list=False,
                document_type="hotărâre a adunării generale",
                reason="este o hotărâre AGA, nu o listă de plată",
            )
        ),
    )
    assert outcome.status == "rejected"
    assert not outcome.is_payment_list
    assert not outcome.should_continue
    assert outcome.document_type == "hotărâre a adunării generale"


def test_rejection_carries_a_reason(config, pdf):
    outcome = triage(
        pdf, config, FakeClient(verdict(is_payment_list=False, reason="este o factură"))
    )
    assert outcome.reason == "este o factură"
    assert outcome.to_dict()["reason"] == "este o factură"


# --------------------------------------------------------------------------
# Fail open
# --------------------------------------------------------------------------


def test_model_error_lets_the_document_through(config, pdf):
    outcome = triage(pdf, config, FakeClient(error=RuntimeError("model căzut")))
    assert outcome.status == "unavailable"
    assert outcome.is_payment_list
    assert outcome.should_continue
    assert "RuntimeError" in outcome.error


def test_unreadable_pdf_lets_the_document_through(config, tmp_path):
    broken = tmp_path / "stricat.pdf"
    broken.write_bytes(b"nu este un PDF")
    outcome = triage(broken, config, FakeClient(verdict()))
    assert outcome.status == "unavailable"
    assert outcome.should_continue


def test_invalid_model_response_lets_the_document_through(config, pdf):
    class Broken(FakeClient):
        def generate_content(self, **kwargs):
            class Response:
                parsed = None
                text = "{nu e json}"

            return Response()

    outcome = triage(pdf, config, Broken())
    assert outcome.status == "unavailable"
    assert outcome.should_continue


def test_disabled_triage_lets_everything_through(pdf):
    config = Config({**TRIAGE_CONFIG, "triage": {**TRIAGE_CONFIG["triage"], "enabled": False}})
    client = FakeClient(verdict(is_payment_list=False))
    outcome = triage(pdf, config, client)
    assert outcome.status == "unavailable"
    assert outcome.should_continue
    assert client.calls == 0, "triajul dezactivat nu trebuie să cheme modelul"


def test_missing_config_key_does_not_silently_pass(pdf):
    """Configul incomplet e o eroare de operare, nu un motiv de fail open tăcut."""
    config = Config({"triage": {"enabled": True}})
    with pytest.raises(ConfigError, match="model"):
        triage(pdf, config, FakeClient(verdict()))


# --------------------------------------------------------------------------
# Costul
# --------------------------------------------------------------------------


def test_only_the_first_page_is_sent(config, pdf, monkeypatch):
    import consilium.triage as triage_module

    seen: dict[str, object] = {}
    original = triage_module.render_first_page

    def spy(path, dpi):
        seen["dpi"] = dpi
        return original(path, dpi)

    monkeypatch.setattr(triage_module, "render_first_page", spy)
    triage(pdf, config, FakeClient(verdict()))
    assert seen["dpi"] == 110


def test_triage_makes_exactly_one_model_call(config, pdf):
    client = FakeClient(verdict())
    triage(pdf, config, client)
    assert client.calls == 1


def test_outcome_serializes_for_firestore(config, pdf):
    stored = triage(pdf, config, FakeClient(verdict())).to_dict()
    assert set(stored) == {
        "status", "is_payment_list", "document_type",
        "confidence", "reason", "model", "error",
    }
    assert stored["model"] == "model-de-test"


def test_unavailable_outcome_is_not_a_rejection():
    outcome = TriageOutcome(is_payment_list=True, status="unavailable")
    assert outcome.should_continue

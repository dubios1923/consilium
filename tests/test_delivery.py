"""Livrarea scrisorii pe email. Complet offline: provider-ul e mock-uit.

Livrarea e ultimul pas și cel mai puțin important. Testele de aici apără două
invariante: nu poate rupe un audit reușit, și nu poate inventa o cifră.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from consilium.delivery import (
    DEFAULT_PROVIDER_URL,
    ENV_API_KEY,
    ENV_RECIPIENT,
    DeliveryConfig,
    DeliveryOutcome,
    build_payload,
    build_subject,
    build_summary,
    deliver,
    verify_summary,
)
from consilium.drafter import format_money
from consilium.reconciler import audit
from tests.conftest import apartment, expense, payment

RECIPIENT = "destinatar@example.org"
FULL_ENV = {ENV_RECIPIENT: RECIPIENT, ENV_API_KEY: "cheie-de-test"}


@pytest.fixture
def case(config):
    document = payment(
        [
            apartment("1", {"A": 60.0}, cota=60.0, arrears=1000.0, penalties=150.0),
            apartment("2", {"A": 40.0}, cota=40.0),
        ],
        [expense("A", 100.0, key="cota_indiviza")],
        total_general=1000.0,
    )
    return document, audit(document, config)


@pytest.fixture
def letter(tmp_path) -> Path:
    path = tmp_path / "cerere.pdf"
    path.write_bytes(b"%PDF-1.4 fals, dar suficient pentru atasament\n")
    return path


class Recorder:
    """Transport fals: reține ce s-a trimis, întoarce ce i se cere."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"id": "msg-123"}
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url, headers, payload):
        self.calls.append((url, headers, payload))
        return self.status, self.body


def exploding(url, headers, payload):
    raise ConnectionError("providerul nu răspunde")


# --------------------------------------------------------------------------
# Activarea
# --------------------------------------------------------------------------


def test_delivery_is_disabled_without_configuration():
    assert DeliveryConfig.from_env({}) is None


@pytest.mark.parametrize("key", [ENV_RECIPIENT, ENV_API_KEY])
def test_half_a_configuration_is_no_configuration(key):
    """O singură variabilă setată nu înseamnă livrare parțială."""
    assert DeliveryConfig.from_env({key: "ceva"}) is None


def test_full_configuration_enables_delivery():
    config = DeliveryConfig.from_env(FULL_ENV)
    assert config is not None
    assert config.recipient == RECIPIENT
    assert config.provider_url == DEFAULT_PROVIDER_URL


def test_disabled_delivery_is_skipped_not_failed(case, letter):
    document, result = case
    outcome = deliver(document, result, [], letter, None)
    assert outcome.status == "skipped"
    assert outcome.message_id is None


# --------------------------------------------------------------------------
# Eșecul nu propagă
# --------------------------------------------------------------------------


def test_transport_exception_does_not_propagate(case, letter):
    document, result = case
    outcome = deliver(
        document, result, [], letter, DeliveryConfig.from_env(FULL_ENV), exploding
    )
    assert outcome.status == "failed"
    assert "ConnectionError" in outcome.error
    assert outcome.recipient == RECIPIENT


def test_provider_error_response_is_recorded_not_raised(case, letter):
    document, result = case
    transport = Recorder(status=422, body={"message": "domeniu neverificat"})
    outcome = deliver(
        document, result, [], letter, DeliveryConfig.from_env(FULL_ENV), transport
    )
    assert outcome.status == "failed"
    assert "422" in outcome.error
    assert "domeniu neverificat" in outcome.error


def test_missing_attachment_fails_without_raising(case, tmp_path):
    document, result = case
    outcome = deliver(
        document,
        result,
        [],
        tmp_path / "inexistent.pdf",
        DeliveryConfig.from_env(FULL_ENV),
        Recorder(),
    )
    assert outcome.status == "failed"
    assert "FileNotFoundError" in outcome.error


def test_successful_delivery_reports_the_message_id(case, letter):
    document, result = case
    transport = Recorder(body={"id": "msg-abc"})
    outcome = deliver(
        document, result, [], letter, DeliveryConfig.from_env(FULL_ENV), transport
    )
    assert outcome.status == "delivered"
    assert outcome.message_id == "msg-abc"
    assert len(transport.calls) == 1


# --------------------------------------------------------------------------
# Rezumatul nu inventează cifre
# --------------------------------------------------------------------------


def test_summary_quotes_only_computed_amounts(case):
    document, result = case
    summary = build_summary(document, result, ["Facturile furnizorilor."])
    assert verify_summary(summary, result, document) == []


def test_summary_contains_the_total_and_every_finding(case):
    document, result = case
    summary = build_summary(document, result, [])
    assert f"{len(result.findings)} constatări" in summary
    assert format_money(result.total_amount_involved) in summary
    for finding in result.findings:
        assert finding.rule_id in summary


def test_an_amount_outside_the_findings_is_caught(case):
    document, result = case
    tampered = build_summary(document, result, []) + "\nPrejudiciu total: 12.345,67 lei"
    violations = verify_summary(tampered, result, document)
    assert len(violations) == 1
    assert "12.345,67" in violations[0]


def test_delivery_is_refused_when_the_summary_would_lie(case, letter, monkeypatch):
    """Dacă rezumatul nu trece verificarea, emailul nu pleacă."""
    import consilium.delivery as delivery_module

    document, result = case
    monkeypatch.setattr(
        delivery_module,
        "build_summary",
        lambda *a, **k: "Prejudiciu inventat: 99.999,99 lei",
    )
    transport = Recorder()
    outcome = deliver(
        document, result, [], letter, DeliveryConfig.from_env(FULL_ENV), transport
    )
    assert outcome.status == "failed"
    assert "DeliveryRefused" in outcome.error
    assert transport.calls == [], "nu trebuie să se fi trimis nimic"


def test_summary_uses_no_model(case):
    """Rezumatul e template pur: modulul nu importă niciun SDK de model."""
    import ast

    tree = ast.parse(Path("consilium/delivery.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"google", "genai", "openai", "anthropic"})


# --------------------------------------------------------------------------
# Ce se trimite
# --------------------------------------------------------------------------


def test_payload_carries_the_pdf_as_base64_attachment(case, letter):
    import base64

    config = DeliveryConfig.from_env(FULL_ENV)
    payload = build_payload(config, "subiect", "rezumat", letter)
    assert payload["to"] == [RECIPIENT]
    attachment = payload["attachments"][0]
    assert attachment["filename"] == "cerere.pdf"
    assert base64.b64decode(attachment["content"]) == letter.read_bytes()


def test_api_key_travels_in_the_header_not_the_body(case, letter):
    document, result = case
    transport = Recorder()
    deliver(
        document, result, [], letter, DeliveryConfig.from_env(FULL_ENV), transport
    )
    _, headers, payload = transport.calls[0]
    assert headers["Authorization"] == "Bearer cheie-de-test"
    assert "cheie-de-test" not in str(payload)


def test_subject_names_the_document_and_the_finding_count(case):
    document, result = case
    subject = build_subject(document, result)
    assert document.document_id in subject
    assert str(len(result.findings)) in subject


def test_outcome_serializes_for_firestore():
    outcome = DeliveryOutcome(status="delivered", recipient=RECIPIENT, message_id="x")
    stored = outcome.to_dict()
    assert stored["status"] == "delivered"
    assert stored["recipient"] == RECIPIENT
    assert isinstance(stored["at"], float)


# --------------------------------------------------------------------------
# Fără chei în repo
# --------------------------------------------------------------------------

# Prefixul urmat de un bloc alfanumeric continuu: un nume de test ca
# `..._are_always_requested` conține „re_” dar are underscore imediat după, deci
# nu se potrivește. Tiparul trebuie să prindă chei reale, nu identificatori.
SECRET_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9_])re_[A-Za-z0-9]{20,}"),   # Resend
    re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9]{20,}"),   # OpenAI
    re.compile(r"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_-]{30,}"),  # Google
    re.compile(r"Bearer\s+[A-Za-z0-9_-]{20,}"),
]

# Control pozitiv: chei plauzibile pe care scanerul TREBUIE să le prindă.
# Fără el, un tipar prea strâns ar trece toate testele fără să apere nimic.
FAKE_KEYS = [
    "re_" + "A1b2C3d4E5f6G7h8I9j0",
    "sk-" + "abcdefghijklmnopqrstuvwxyz12",
    "AIza" + "SyD0123456789abcdefghijklmnopqrstuv",
    "Bearer " + "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
]

TRACKED_TEXT = [
    *Path("consilium").glob("*.py"),
    *Path("job").glob("*.py"),
    *Path("tools").glob("*.py"),
    *Path("tests").glob("*.py"),
    *Path("scripts").glob("*.sh"),
    Path("config.yaml"),
    Path("Dockerfile"),
]


@pytest.mark.parametrize("fake", FAKE_KEYS)
def test_the_scanner_actually_detects_a_key(fake):
    """Fără acest test, un tipar prea strâns ar trece verde apărând nimic."""
    assert any(pattern.search(fake) for pattern in SECRET_PATTERNS)


@pytest.mark.parametrize("path", TRACKED_TEXT, ids=lambda p: str(p))
def test_no_api_keys_committed(path):
    content = path.read_text(encoding="utf-8")
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(content), f"posibil secret în {path}"


def test_config_yaml_has_no_delivery_credentials():
    content = Path("config.yaml").read_text(encoding="utf-8")
    lowered = content.lower()
    for forbidden in ("api_key", "apikey", "password", "secret", "token"):
        assert forbidden not in lowered, f"`{forbidden}` nu are ce căuta în config"

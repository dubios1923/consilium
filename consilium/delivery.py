"""Livrarea scrisorii pe email, ca pas final de output.

Modul izolat: nu importă reconciler-ul, nu atinge nicio regulă și nu recalculează
nimic. Primește constatările deja calculate și scrisoarea deja verificată, și le
trimite mai departe.

Rezumatul din corpul emailului este pur template: niciun model nu îl scrie.
Cifrele din el sunt verificate cu aceeași listă de sume permise ca scrisoarea:
dacă o sumă nu provine dintr-o constatare calculată, livrarea e refuzată.

Cheia de API se citește exclusiv din mediu. Nu are ce căuta în `config.yaml`,
care e versionat.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from consilium.drafter import (
    MONEY_PATTERN,
    allowed_amounts,
    format_money,
    parse_money,
)
from consilium.reconciler import AuditResult
from consilium.schema import PaymentList

DEFAULT_PROVIDER_URL = "https://api.resend.com/emails"
DEFAULT_SENDER = "Consilium <consilium@datahappens.ro>"
REQUEST_TIMEOUT_SECONDS = 20
# Fără un User-Agent propriu, Cloudflare din fața API-ului respinge cererea
# cu 403 / error code 1010, pe semnătura implicită a urllib.
USER_AGENT = "consilium/1.0 (+https://github.com/dubios1923/consilium)"

ENV_RECIPIENT = "CONSILIUM_DELIVERY_TO"
ENV_API_KEY = "CONSILIUM_DELIVERY_API_KEY"
# Numele nativ al providerului, acceptat ca alias: deploy-ul citește
# RESEND_API_KEY din mediu și îl montează în job sub numele canonic, iar o
# rulare locală trebuie să meargă cu același fișier de secrete.
ENV_API_KEY_ALIAS = "RESEND_API_KEY"
ENV_SENDER = "CONSILIUM_DELIVERY_FROM"
ENV_PROVIDER_URL = "CONSILIUM_DELIVERY_URL"

DeliveryStatus = Literal["delivered", "failed", "skipped"]


class DeliveryRefused(RuntimeError):
    """Rezumatul citează o sumă care nu provine dintr-o constatare calculată."""


@dataclass(frozen=True)
class DeliveryConfig:
    """Configurația livrării. Absentă înseamnă livrare dezactivată."""

    recipient: str
    api_key: str
    sender: str = DEFAULT_SENDER
    provider_url: str = DEFAULT_PROVIDER_URL

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> DeliveryConfig | None:
        """Construiește configul din mediu, sau None dacă livrarea e dezactivată.

        Ambele variabile sunt obligatorii. Una singură setată înseamnă
        configurare incompletă, nu livrare parțială: se raportează ca dezactivat.
        """
        source = os.environ if env is None else env
        recipient = (source.get(ENV_RECIPIENT) or "").strip()
        api_key = (
            source.get(ENV_API_KEY) or source.get(ENV_API_KEY_ALIAS) or ""
        ).strip()
        if not recipient or not api_key:
            return None
        return cls(
            recipient=recipient,
            api_key=api_key,
            sender=(source.get(ENV_SENDER) or DEFAULT_SENDER).strip(),
            provider_url=(
                source.get(ENV_PROVIDER_URL) or DEFAULT_PROVIDER_URL
            ).strip(),
        )


@dataclass(frozen=True)
class DeliveryOutcome:
    """Ce s-a întâmplat cu livrarea. Nu poate face auditul să eșueze."""

    status: DeliveryStatus
    recipient: str | None = None
    message_id: str | None = None
    error: str | None = None
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "recipient": self.recipient,
            "message_id": self.message_id,
            "error": self.error,
            "at": self.at,
        }


# --------------------------------------------------------------------------
# Rezumatul
# --------------------------------------------------------------------------


def _finding_label(finding: Any) -> str:
    label = finding.rule_id
    if finding.apartment_no:
        label += f", apartamentul {finding.apartment_no}"
    if finding.category:
        label += f", „{finding.category}”"
    return f"[{label}]"


def build_summary(
    payment: PaymentList,
    result: AuditResult,
    requested_documents: list[str],
    audit_id: str = "",
) -> str:
    """Rezumatul din corpul emailului. Template pur, fără model.

    Fiecare cifră provine dintr-un câmp al unei constatări sau din raportul de
    acoperire. Nimic nu e recalculat aici.
    """
    findings = result.findings
    lines = [
        f"Audit {payment.document_id}",
        f"{payment.association_ref}, perioada {payment.period}",
        "",
    ]

    if findings:
        lines.append(
            f"{len(findings)} constatări, sumă totală implicată: "
            f"{format_money(result.total_amount_involved)} lei"
        )
        lines.append("")
        for finding in findings:
            amount = (
                f": {format_money(finding.amount_involved)} lei"
                if finding.amount_involved is not None
                else ""
            )
            lines.append(f"  {_finding_label(finding)}{amount}")
    else:
        lines.append("Nicio constatare: lista de plată este consistentă.")
    lines.append("")

    references = list(
        dict.fromkeys(
            finding.legal_reference
            for finding in findings
            if finding.legal_reference
        )
    )
    if references:
        lines.append("Temeiuri legale invocate:")
        lines.extend(f"  {reference}" for reference in references)
        lines.append("")

    lines.append(f"Acoperire: {result.coverage.summary()}.")
    for category, reason in result.coverage.expense_lines_unverified:
        lines.append(f"  neverificat, {category}: {reason}")
    lines.append("")

    if requested_documents:
        lines.append(f"Documente solicitate ({len(requested_documents)}):")
        for index, document in enumerate(requested_documents, start=1):
            lines.append(f"  {index}. {document}")
        lines.append("")

    lines.append("Scrisoarea completă este atașată ca PDF.")
    if audit_id:
        lines.append(f"Dosar de verificare: {audit_id}")
    return "\n".join(lines)


def verify_summary(
    summary: str, result: AuditResult, payment: PaymentList
) -> list[str]:
    """Aceeași regulă ca la scrisoare: nicio sumă din afara constatărilor."""
    allowed = allowed_amounts(result, payment)
    return [
        f"suma {token} nu provine din nicio constatare calculată"
        for token in MONEY_PATTERN.findall(summary)
        if parse_money(token) not in allowed
    ]


def build_subject(payment: PaymentList, result: AuditResult) -> str:
    count = len(result.findings)
    if count == 0:
        return f"Consilium, {payment.document_id}: nicio constatare"
    return (
        f"Consilium, {payment.document_id}: {count} constatări "
        f"({payment.period})"
    )


# --------------------------------------------------------------------------
# Transportul
# --------------------------------------------------------------------------

# (url, headers, payload) -> (cod HTTP, corp decodat)
Transport = Callable[[str, dict[str, str], dict[str, Any]], tuple[int, dict[str, Any]]]


def http_transport(
    url: str, headers: dict[str, str], payload: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Transportul real, peste stdlib. Nicio dependență nouă."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            **headers,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            body = response.read().decode("utf-8") or "{}"
            return response.status, json.loads(body)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail or "{}")
        except json.JSONDecodeError:
            parsed = {"message": detail}
        return error.code, parsed


def build_payload(
    config: DeliveryConfig,
    subject: str,
    summary: str,
    pdf_path: Path,
) -> dict[str, Any]:
    attachment = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
    return {
        "from": config.sender,
        "to": [config.recipient],
        "subject": subject,
        "text": summary,
        "attachments": [{"filename": pdf_path.name, "content": attachment}],
    }


def deliver(
    payment: PaymentList,
    result: AuditResult,
    requested_documents: list[str],
    pdf_path: str | Path,
    config: DeliveryConfig | None,
    transport: Transport | None = None,
    audit_id: str = "",
) -> DeliveryOutcome:
    """Trimite scrisoarea. Nu ridică niciodată: întoarce rezultatul, oricare ar fi.

    Livrarea e ultimul pas și cel mai puțin important: artefactele sunt deja în
    GCS și constatările deja în Firestore. Un provider căzut nu are voie să
    invalideze un audit care a reușit.
    """
    if config is None:
        return DeliveryOutcome(status="skipped", error="livrare neconfigurată")

    try:
        summary = build_summary(payment, result, requested_documents, audit_id)
        violations = verify_summary(summary, result, payment)
        if violations:
            raise DeliveryRefused("; ".join(violations))

        path = Path(pdf_path)
        if not path.is_file():
            raise FileNotFoundError(f"scrisoarea lipsește: {path}")

        send = transport or http_transport
        status_code, body = send(
            config.provider_url,
            {"Authorization": f"Bearer {config.api_key}"},
            build_payload(config, build_subject(payment, result), summary, path),
        )
        if status_code >= 400:
            detail = body.get("message") or body.get("error") or str(body)
            return DeliveryOutcome(
                status="failed",
                recipient=config.recipient,
                error=f"HTTP {status_code}: {detail}",
            )
        return DeliveryOutcome(
            status="delivered",
            recipient=config.recipient,
            message_id=str(body.get("id") or "") or None,
        )
    except Exception as error:  # noqa: BLE001 - livrarea nu poate rupe auditul
        return DeliveryOutcome(
            status="failed",
            recipient=config.recipient,
            error=f"{type(error).__name__}: {error}",
        )

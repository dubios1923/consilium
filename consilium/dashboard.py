"""Pagina de inspecție a auditurilor. Read-only, ca și agentul ADK.

Nu declanșează nimic și nu modifică nimic: citește din Firestore și servește
scrisoarea din GCS. Un audit se pornește punând un PDF în bucket, nu apăsând un
buton — dacă pagina ar putea executa, un refresh într-un demo ar porni procesări
în paralel pe același document.

Lista se reîmprospătează singură cât timp există audituri în lucru, ca să se
poată urmări pipeline-ul trecând prin etape.
"""

from __future__ import annotations

import html
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from consilium.state import AuditRecord, FirestoreAuditStore

app = FastAPI(title="Consilium — inspecție audituri")

STATUS_LABELS = {
    "queued": "în așteptare",
    "triaging": "triaj",
    "extracting": "extragere",
    "verifying": "verificare R0",
    "reconciling": "reconciliere",
    "drafting": "redactare",
    "delivering": "livrare",
    "done": "finalizat",
    "rejected": "respins",
    "failed": "eșuat",
}

RUNNING = {"queued", "triaging", "extracting", "verifying", "reconciling",
           "drafting", "delivering"}

STEP_LABELS = {
    "triage": "Triaj",
    "extract": "Extragere",
    "verify": "Verificare R0",
    "reconcile": "Reconciliere",
    "draft": "Redactare",
    "deliver": "Livrare",
}

SEVERITY_LABELS = {"high": "ridicată", "medium": "medie", "low": "scăzută",
                   "info": "informativă"}


def store() -> FirestoreAuditStore:
    return FirestoreAuditStore(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def money(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    text = f"{float(value):,.2f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".") + " lei"


def when(timestamp: float | None) -> str:
    if not timestamp:
        return "—"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
        "%d.%m.%Y %H:%M"
    )


CSS = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --ink: #16181d; --muted: #61656e;
  --line: #dfe3e8; --accent: #2d5a4a; --warn: #8b3a3a; --info: #3a4a6b;
  --band: #eef1f4;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, "Segoe UI", Roboto, "Liberation Sans", sans-serif;
}
a { color: var(--accent); }
.wrap { max-width: 1080px; margin: 0 auto; padding: 32px 24px 64px; }
header.top { border-bottom: 1px solid var(--line); padding-bottom: 18px; margin-bottom: 26px; }
h1 { font-size: 21px; margin: 0 0 4px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 13.5px; }
h2 { font-size: 15px; margin: 30px 0 12px; text-transform: uppercase;
     letter-spacing: 0.06em; color: var(--muted); }
.stats { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 8px; }
.stat { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
        padding: 14px 18px; min-width: 150px; }
.stat .n { font-size: 24px; font-weight: 600; letter-spacing: -0.02em; }
.stat .l { color: var(--muted); font-size: 12.5px; margin-top: 2px; }
table { width: 100%; border-collapse: collapse; background: var(--card);
        border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line);
         font-size: 13.5px; vertical-align: top; }
th { background: var(--band); font-weight: 600; color: var(--muted);
     text-transform: uppercase; font-size: 11.5px; letter-spacing: 0.05em; }
tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.pill { display: inline-block; padding: 2px 9px; border-radius: 999px;
        font-size: 11.5px; font-weight: 600; letter-spacing: 0.02em; }
.pill.done { background: #e2efe9; color: #1c3b30; }
.pill.rejected { background: #e9eaee; color: #4a4f5a; }
.pill.failed { background: #f6e3e3; color: #7a2f2f; }
.pill.run { background: #e6ebf5; color: #2b3a55; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
        padding: 16px 18px; margin-bottom: 12px; }
.card .head { display: flex; justify-content: space-between; gap: 16px;
              align-items: baseline; margin-bottom: 6px; }
.rule { font-weight: 600; font-size: 14px; }
.amount { font-variant-numeric: tabular-nums; font-weight: 600; white-space: nowrap; }
.amount.warn { color: var(--warn); }
.msg { font-size: 13.5px; }
.legal { color: var(--muted); font-size: 12.5px; margin-top: 8px;
         border-top: 1px dashed var(--line); padding-top: 8px; }
.sev { font-size: 11.5px; color: var(--muted); }
.timeline { display: flex; flex-wrap: wrap; gap: 8px; }
.step { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
        padding: 9px 13px; font-size: 12.5px; min-width: 120px; }
.step.ok { border-left: 3px solid var(--accent); }
.step.failed { border-left: 3px solid var(--warn); }
.step .d { color: var(--muted); font-variant-numeric: tabular-nums; }
.note { color: var(--muted); font-size: 13px; }
.empty { color: var(--muted); padding: 22px; text-align: center;
         background: var(--card); border: 1px dashed var(--line); border-radius: 10px; }
.back { font-size: 13px; }
code { background: var(--band); padding: 1px 5px; border-radius: 4px; font-size: 12.5px; }
ul.docs { margin: 0; padding-left: 20px; font-size: 13.5px; }
ul.docs li { margin-bottom: 6px; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #101216; --card: #181b21; --ink: #e8eaee; --muted: #9298a4;
          --line: #2a2f38; --band: #1f232b; --accent: #6fb39a; --warn: #d98a8a; }
  .pill.done { background: #1e3830; color: #8fd3b8; }
  .pill.rejected { background: #262a33; color: #a8aebb; }
  .pill.failed { background: #3a2323; color: #e0a0a0; }
  .pill.run { background: #232b3a; color: #9db4dd; }
}
"""


def page(title: str, body: str, refresh: bool = False, status: int = 200) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="5">' if refresh else ""
    return HTMLResponse(
        status_code=status,
        content=
        f"""<!doctype html><html lang="ro"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>{meta}<style>{CSS}</style></head>
<body><div class="wrap">{body}</div></body></html>""",
    )


def status_pill(status: str) -> str:
    css = "run" if status in RUNNING else status
    return f'<span class="pill {css}">{e(STATUS_LABELS.get(status, status))}</span>'


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    try:
        records = store().list_recent(limit=50)
    except Exception as error:  # noqa: BLE001 - pagina nu are voie sa cada
        return page(
            "Consilium",
            f'<div class="empty">Firestore indisponibil: {e(error)}</div>',
            status=503,
        )

    audited = [r for r in records if r.status == "done"]
    findings = sum(len(r.findings) for r in audited)
    involved = sum(
        finding.get("amount_involved") or 0.0
        for r in audited
        for finding in r.findings
        if finding.get("severity") != "info"
    )
    rejected = sum(1 for r in records if r.status == "rejected")

    rows = []
    for record in records:
        count = len(record.findings)
        rows.append(
            f"""<tr>
<td><a href="/audit/{e(record.audit_id)}"><code>{e(record.audit_id)}</code></a></td>
<td>{e(record.association_ref or "—")}<div class="sev">{e(record.source_uri.rsplit("/", 1)[-1])}</div></td>
<td>{e(record.period or "—")}</td>
<td>{status_pill(record.status)}</td>
<td class="num">{count if record.status == "done" else "—"}</td>
<td class="num">{when(record.created_at)}</td></tr>"""
        )

    table = (
        "<table><tr><th>Dosar</th><th>Asociație / fișier</th><th>Perioadă</th>"
        "<th>Stare</th><th class='num'>Constatări</th><th class='num'>Creat</th></tr>"
        + "".join(rows)
        + "</table>"
    ) if rows else '<div class="empty">Niciun audit încă. Încarcă un PDF în bucket-ul de intrare.</div>'

    running = any(r.status in RUNNING for r in records)
    body = f"""<header class="top">
<h1>Consilium — audituri</h1>
<div class="sub">Liste de plată ale asociațiilor de proprietari. Pagină de inspecție, read-only.
{' Se reîmprospătează automat.' if running else ''}</div>
</header>
<div class="stats">
  <div class="stat"><div class="n">{len(records)}</div><div class="l">audituri</div></div>
  <div class="stat"><div class="n">{findings}</div><div class="l">constatări</div></div>
  <div class="stat"><div class="n">{money(involved)}</div><div class="l">sume implicate</div></div>
  <div class="stat"><div class="n">{rejected}</div><div class="l">respinse la triaj</div></div>
</div>
<h2>Dosare</h2>{table}"""
    return page("Consilium — audituri", body, refresh=running)


def render_triage(record: AuditRecord) -> str:
    triage = record.triage
    if not triage:
        return ""
    status = triage.get("status")
    if status == "rejected":
        head = "Document respins la intrare"
        detail = (
            f"Tip identificat: <b>{e(triage.get('document_type'))}</b> "
            f"(încredere {e(triage.get('confidence'))}). "
            f"Pipeline-ul de extragere nu a fost pornit."
        )
    elif status == "unavailable":
        head = "Triaj indisponibil — document trecut mai departe"
        detail = e(triage.get("error") or triage.get("reason"))
    else:
        head = "Acceptat la triaj"
        detail = (
            f"Tip identificat: <b>{e(triage.get('document_type'))}</b> "
            f"(încredere {e(triage.get('confidence'))})."
        )
    return f"""<h2>Triaj</h2><div class="card">
<div class="rule">{e(head)}</div>
<div class="msg">{detail}</div>
<div class="legal">{e(triage.get('reason'))} · model <code>{e(triage.get('model'))}</code></div>
</div>"""


def render_findings(record: AuditRecord) -> str:
    if not record.findings:
        if record.status == "done":
            return ('<h2>Constatări</h2><div class="empty">'
                    "Nicio constatare: lista de plată este consistentă.</div>")
        return ""
    cards = []
    for finding in record.findings:
        label = e(finding.get("rule_id"))
        if finding.get("apartment_no"):
            label += f", apartamentul {e(finding['apartment_no'])}"
        if finding.get("category"):
            label += f", „{e(finding['category'])}”"
        amount = finding.get("amount_involved")
        severity = finding.get("severity", "")
        amount_html = (
            f'<span class="amount {"warn" if severity != "info" else ""}">{money(amount)}</span>'
            if isinstance(amount, (int, float))
            else '<span class="sev">fără sumă</span>'
        )
        legal = finding.get("legal_reference")
        cards.append(
            f"""<div class="card">
<div class="head"><span class="rule">{label}</span>{amount_html}</div>
<div class="msg">{e(finding.get('message'))}</div>
<div class="sev">severitate {e(SEVERITY_LABELS.get(severity, severity))}</div>
{f'<div class="legal">Temei: {e(legal)}</div>' if legal else ''}
</div>"""
        )
    return "<h2>Constatări</h2>" + "".join(cards)


def render_coverage(record: AuditRecord) -> str:
    coverage = record.coverage_report
    if not coverage:
        return ""
    rules = "".join(
        f"<tr><td><b>{e(rule.get('rule_id'))}</b></td><td>{e(rule.get('status'))}</td>"
        f"<td class='num'>{e(rule.get('checked'))}/{e(rule.get('total'))}</td>"
        f"<td>{e(rule.get('reason') or '—')}</td></tr>"
        for rule in coverage.get("rules", [])
    )
    unverified = "".join(
        f"<li><b>{e(item.get('category'))}</b> — {e(item.get('reason'))}</li>"
        for item in coverage.get("expense_lines_unverified", [])
    )
    docs = "".join(
        f"<li>{e(document)}</li>"
        for document in coverage.get("documents_to_request", [])
    )
    return f"""<h2>Acoperire</h2>
<div class="card"><div class="rule">{e(coverage.get('summary'))}</div>
{f'<ul class="docs">{unverified}</ul>' if unverified else ''}</div>
<table><tr><th>Regulă</th><th>Stare</th><th class="num">Verificat</th><th>Motiv</th></tr>
{rules}</table>
{f'<h2>Documente solicitate</h2><div class="card"><ul class="docs">{docs}</ul></div>' if docs else ''}"""


def render_timeline(record: AuditRecord) -> str:
    steps = []
    for entry in record.step_log:
        if entry.get("status") == "started":
            continue
        css = "failed" if entry.get("status") == "failed" else "ok"
        duration = entry.get("duration_s")
        steps.append(
            f"""<div class="step {css}"><div>{e(STEP_LABELS.get(entry['step'], entry['step']))}</div>
<div class="d">{f'{duration:.2f} s' if isinstance(duration, (int, float)) else '—'}</div></div>"""
        )
    if not steps:
        return ""
    return f'<h2>Etape</h2><div class="timeline">{"".join(steps)}</div>'


def render_delivery(record: AuditRecord) -> str:
    delivery = record.delivery
    if not delivery:
        return ""
    status = delivery.get("status")
    text = {
        "delivered": f"Trimis către {e(delivery.get('recipient'))}",
        "skipped": "Livrare dezactivată",
        "failed": f"Eșuat: {e(delivery.get('error'))}",
    }.get(status, e(status))
    identifier = delivery.get("message_id")
    return f"""<h2>Livrare</h2><div class="card"><div class="msg">{text}</div>
{f'<div class="legal">Mesaj <code>{e(identifier)}</code></div>' if identifier else ''}</div>"""


@app.get("/audit/{audit_id}", response_class=HTMLResponse)
def detail(audit_id: str) -> HTMLResponse:
    try:
        record = store().get(audit_id)
    except Exception as error:  # noqa: BLE001
        return page(
            "Consilium",
            f'<div class="empty">Firestore indisponibil: {e(error)}</div>',
            status=503,
        )
    if record is None:
        return page(
            "Consilium", '<div class="empty">Audit inexistent.</div>', status=404
        )

    letter = next((u for u in record.artifact_uris if u.endswith(".pdf")), None)
    links = []
    if letter:
        links.append(f'<a href="/audit/{e(audit_id)}/letter">Scrisoarea (PDF)</a>')
    links += [f"<code>{e(uri)}</code>" for uri in record.artifact_uris]
    artifacts = (
        '<h2>Artefacte</h2><div class="card"><ul class="docs">'
        + "".join(f"<li>{item}</li>" for item in links)
        + "</ul></div>"
    ) if links else ""

    body = f"""<header class="top">
<div class="back"><a href="/">← toate auditurile</a></div>
<h1>{e(record.association_ref or record.source_uri.rsplit("/", 1)[-1])}</h1>
<div class="sub">{status_pill(record.status)} &nbsp; <code>{e(record.audit_id)}</code>
&nbsp;·&nbsp; {e(record.document_id or "document neidentificat")}
&nbsp;·&nbsp; perioada {e(record.period or "—")}
&nbsp;·&nbsp; {when(record.created_at)}</div>
{f'<div class="sub" style="color:var(--warn);margin-top:6px">Eroare: {e(record.error)}</div>' if record.error else ''}
</header>
{render_triage(record)}
{render_findings(record)}
{render_coverage(record)}
{render_timeline(record)}
{render_delivery(record)}
{artifacts}
<div class="note" style="margin-top:26px">Sursă: <code>{e(record.source_uri)}</code></div>"""
    return page(
        f"Consilium — {record.audit_id}", body, refresh=record.status in RUNNING
    )


@app.get("/audit/{audit_id}/letter")
def letter(audit_id: str) -> Response:
    """Servește scrisoarea din GCS. Citire, nu semnare de URL."""
    try:
        record = store().get(audit_id)
    except Exception as error:  # noqa: BLE001
        return Response(f"Firestore indisponibil: {error}", status_code=503)
    if record is None:
        return Response("Audit inexistent", status_code=404)

    uri = next((u for u in record.artifact_uris if u.endswith(".pdf")), None)
    if not uri:
        return Response("Auditul nu are scrisoare", status_code=404)

    from google.cloud import storage

    bucket_name, _, blob_path = uri[5:].partition("/")
    try:
        data = storage.Client().bucket(bucket_name).blob(blob_path).download_as_bytes()
    except Exception as error:  # noqa: BLE001
        return Response(f"Artefact inaccesibil: {error}", status_code=502)
    return Response(
        data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{blob_path.rsplit("/", 1)[-1]}"'
        },
    )

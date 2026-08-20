"""Pagina de inspecție a auditurilor. Read-only, ca și agentul ADK.

Nu modifică niciun audit existent: citește din Firestore și servește scrisoarea
din GCS. Un refresh nu poate reporni o procesare, oricâte ori s-ar da.

Singura acțiune posibilă este încărcarea unui document nou, care scrie fișierul
în bucket exact ca `gcloud storage cp` și lasă Eventarc să facă restul. Fără ea,
sistemul e utilizabil doar de cineva care are gcloud instalat și drepturi pe
proiect, ceea ce exclude tocmai proprietarul de apartament pentru care e scris.

Lista se reîmprospătează singură cât timp există audituri în lucru, ca să se
poată urmări pipeline-ul trecând prin etape.
"""

from __future__ import annotations

import html
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from consilium.state import AuditRecord, FirestoreAuditStore

app = FastAPI(title="Consilium: inspecție audituri")

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

BUCKET = os.environ.get("CONSILIUM_INTAKE_BUCKET", "consilium-intake-ab7x21")
INTAKE_PREFIX = "liste/"
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
# Pagina e publica, iar fiecare document costa apeluri de model. Plafonul zilnic
# nu apara de un atacator hotarat, dar opreste o factura accidentala.
DAILY_UPLOAD_LIMIT = int(os.environ.get("CONSILIUM_DAILY_UPLOAD_LIMIT", "40"))

# FastAPI cere descriptorul ca valoare implicita; il tinem separat ca sa nu
# apelam File() in semnatura.
UPLOAD_FIELD = File(...)

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



# --------------------------------------------------------------------------
# Limba interfetei
#
# Regulamentul cere ca aplicatia sa suporte engleza. Continutul auditului ramane
# romanesc si trebuie sa ramana: constatarile citeaza un document romanesc si o
# lege romaneasca, iar scrisoarea generata este un act juridic. Se traduce deci
# interfata, nu documentul. Limba se ia din browser, cu comutare manuala.
# --------------------------------------------------------------------------

T: dict[str, dict[str, str]] = {
    "title": {"ro": "Consilium: audituri", "en": "Consilium: audits"},
    "subtitle": {
        "ro": "Liste de plată ale asociațiilor de proprietari. Pagină de inspecție, read-only.",
        "en": "Payment lists issued by Romanian homeowners' associations. Read-only inspection page.",
    },
    "refreshing": {
        "ro": " Se reîmprospătează automat.",
        "en": " Refreshing automatically.",
    },
    "audits": {"ro": "audituri", "en": "audits"},
    "findings": {"ro": "constatări", "en": "findings"},
    "involved": {"ro": "sume implicate", "en": "amounts involved"},
    "rejected_count": {"ro": "respinse la triaj", "en": "rejected at triage"},
    "cases": {"ro": "Dosare", "en": "Cases"},
    "col_case": {"ro": "Dosar", "en": "Case"},
    "col_org": {"ro": "Asociație / fișier", "en": "Association / file"},
    "col_period": {"ro": "Perioadă", "en": "Period"},
    "col_status": {"ro": "Stare", "en": "Status"},
    "col_findings": {"ro": "Constatări", "en": "Findings"},
    "col_created": {"ro": "Creat", "en": "Created"},
    "unknown_org": {"ro": "necunoscută", "en": "unknown"},
    "empty": {
        "ro": "Niciun audit încă. Încarcă o listă de plată mai sus.",
        "en": "No audits yet. Upload a payment list above.",
    },
    "upload_heading": {
        "ro": "Încarcă o listă de plată", "en": "Upload a payment list",
    },
    "upload_button": {"ro": "Pornește auditul", "en": "Start the audit"},
    "upload_hint": {
        "ro": "Doar PDF, cel mult 12 MB. Documentul ajunge în bucket și "
              "pipeline-ul pornește singur. Un document care nu e listă de plată "
              "e respins la triaj în câteva secunde.",
        "en": "PDF only, 12 MB max. The file lands in the intake bucket and the "
              "pipeline starts on its own. A document that is not a payment list "
              "is rejected at triage within seconds.",
    },
    "output_note": {
        "ro": "",
        "en": "Findings and the generated letter stay in Romanian: they quote a "
              "Romanian document and Romanian statute, and the letter is a legal "
              "filing.",
    },
    "back": {"ro": "← toate auditurile", "en": "← all audits"},
    "triage": {"ro": "Triaj", "en": "Triage"},
    "coverage": {"ro": "Acoperire", "en": "Coverage"},
    "documents": {"ro": "Documente solicitate", "en": "Documents requested"},
    "stages": {"ro": "Etape", "en": "Stages"},
    "delivery": {"ro": "Livrare", "en": "Delivery"},
    "artifacts": {"ro": "Artefacte", "en": "Artifacts"},
    "letter_link": {"ro": "Scrisoarea (PDF)", "en": "The letter (PDF)"},
    "source": {"ro": "Sursă", "en": "Source"},
    "error": {"ro": "Eroare", "en": "Error"},
    "no_findings": {
        "ro": "Nicio constatare: lista de plată este consistentă.",
        "en": "No findings: the payment list is internally consistent.",
    },
    "rule": {"ro": "Regulă", "en": "Rule"},
    "checked": {"ro": "Verificat", "en": "Checked"},
    "reason": {"ro": "Motiv", "en": "Reason"},
    "severity": {"ro": "severitate", "en": "severity"},
    "basis": {"ro": "Temei", "en": "Legal basis"},
    "no_amount": {"ro": "fără sumă", "en": "no amount"},
    "apartment": {"ro": "apartamentul", "en": "apartment"},
    "period_of": {"ro": "perioada", "en": "period"},
    "no_document": {
        "ro": "document neidentificat", "en": "document not identified",
    },
    "model": {"ro": "model", "en": "model"},
    "triage_rejected": {
        "ro": "Document respins la intrare",
        "en": "Document rejected at the gate",
    },
    "triage_unavailable": {
        "ro": "Triaj indisponibil, document trecut mai departe",
        "en": "Triage unavailable, document passed through",
    },
    "triage_accepted": {"ro": "Acceptat la triaj", "en": "Accepted at triage"},
    "identified_as": {"ro": "Tip identificat", "en": "Identified as"},
    "confidence": {"ro": "încredere", "en": "confidence"},
    "not_started": {
        "ro": "Pipeline-ul de extragere nu a fost pornit.",
        "en": "The extraction pipeline was never started.",
    },
    "delivered_to": {"ro": "Trimis către", "en": "Sent to"},
    "delivery_off": {"ro": "Livrare dezactivată", "en": "Delivery disabled"},
    "delivery_failed": {"ro": "Eșuat", "en": "Failed"},
    "message": {"ro": "Mesaj", "en": "Message"},
    "switch": {"ro": "English", "en": "Română"},
}

STATUS_LABELS_EN = {
    "queued": "queued", "triaging": "triage", "extracting": "extracting",
    "verifying": "R0 check", "reconciling": "reconciling", "drafting": "drafting",
    "delivering": "delivering", "done": "done", "rejected": "rejected",
    "failed": "failed",
}

STEP_LABELS_EN = {
    "triage": "Triage", "extract": "Extraction", "verify": "R0 check",
    "reconcile": "Reconciliation", "draft": "Drafting", "deliver": "Delivery",
}

# Starile de acoperire vin din reconciler ca text romanesc. Sunt stari de
# interfata, nu continut al documentului, deci se traduc.
COVERAGE_STATUS_EN = {
    "verificat": "verified", "parțial": "partial", "neverificabil": "unverifiable",
}

SEVERITY_LABELS_EN = {
    "high": "high", "medium": "medium", "low": "low", "info": "informational",
}


def pick_language(requested: str | None, header: str | None) -> str:
    """Limba paginii: alegerea explicita, altfel ce cere browserul, altfel engleza."""
    if requested in ("ro", "en"):
        return requested
    if header and header.strip().lower().startswith("ro"):
        return "ro"
    return "en"


def t(key: str, lang: str) -> str:
    return T[key][lang]


def store() -> FirestoreAuditStore:
    return FirestoreAuditStore(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def money(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    text = f"{float(value):,.2f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".") + " lei"


def when(timestamp: float | None) -> str:
    if not timestamp:
        return ""
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
.drop { background: var(--card); border: 1px dashed var(--line); border-radius: 10px;
        padding: 18px 20px; }
.drop input[type=file] { color: var(--muted); font-size: 13.5px; }
.drop .chosen { color: var(--ink); font-size: 13.5px; margin: 8px 0 2px; }
.drop button { margin-top: 10px; background: var(--accent); color: #fff; border: 0;
               border-radius: 7px; padding: 9px 18px; font-size: 13.5px;
               font-weight: 600; cursor: pointer; }
.drop button:disabled { background: var(--band); color: var(--muted); cursor: default; }
.hint { color: var(--muted); font-size: 12.5px; margin-top: 9px; max-width: 62ch; }
.lang { float: right; font-size: 12.5px; color: var(--muted); text-decoration: none;
        border: 1px solid var(--line); border-radius: 6px; padding: 4px 11px; }
.lang:hover { color: var(--ink); }
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


def page(
    title: str, body: str, refresh: bool = False, status: int = 200,
    lang: str = "ro", switch_to: str = "",
) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="5">' if refresh else ""
    toggle = (
        f'<a class="lang" href="{switch_to}">{e(t("switch", lang))}</a>'
        if switch_to
        else ""
    )
    return HTMLResponse(
        status_code=status,
        content=f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>{meta}<style>{CSS}</style></head>
<body><div class="wrap">{toggle}{body}</div></body></html>""",
    )


def status_pill(status: str, lang: str = "ro") -> str:
    css = "run" if status in RUNNING else status
    labels = STATUS_LABELS if lang == "ro" else STATUS_LABELS_EN
    return f'<span class="pill {css}">{e(labels.get(status, status))}</span>'


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, notice: str = "", lang: str = "") -> HTMLResponse:
    lang = pick_language(lang or None, request.headers.get("accept-language"))
    other = "ro" if lang == "en" else "en"
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
<td>{e(record.association_ref) or f'<span class="sev">{e(t("unknown_org", lang))}</span>'}<div class="sev">{e(record.source_uri.rsplit("/", 1)[-1])}</div></td>
<td>{e(record.period or "")}</td>
<td>{status_pill(record.status, lang)}</td>
<td class="num">{count if record.status == "done" else ""}</td>
<td class="num">{when(record.created_at)}</td></tr>"""
        )

    table = (
        f"<table><tr><th>{e(t('col_case', lang))}</th>"
        f"<th>{e(t('col_org', lang))}</th><th>{e(t('col_period', lang))}</th>"
        f"<th>{e(t('col_status', lang))}</th>"
        f"<th class='num'>{e(t('col_findings', lang))}</th>"
        f"<th class='num'>{e(t('col_created', lang))}</th></tr>"
        + "".join(rows)
        + "</table>"
    ) if rows else f'<div class="empty">{e(t("empty", lang))}</div>'

    running = any(r.status in RUNNING for r in records)
    body = f"""<header class="top">
<h1>{e(t("title", lang))}</h1>
<div class="sub">{e(t("subtitle", lang))}{e(t("refreshing", lang)) if running else ''}</div>
</header>
<div class="stats">
  <div class="stat"><div class="n">{len(records)}</div><div class="l">{e(t("audits", lang))}</div></div>
  <div class="stat"><div class="n">{findings}</div><div class="l">{e(t("findings", lang))}</div></div>
  <div class="stat"><div class="n">{money(involved)}</div><div class="l">{e(t("involved", lang))}</div></div>
  <div class="stat"><div class="n">{rejected}</div><div class="l">{e(t("rejected_count", lang))}</div></div>
</div>
<h2>{e(t("upload_heading", lang))}</h2>
<form class="drop" method="post" action="/upload" enctype="multipart/form-data">
  <input type="file" name="document" accept="application/pdf,.pdf" required
         onchange="this.form.querySelector('button').disabled=false;
                   this.form.querySelector('.chosen').textContent=this.files[0].name">
  <div class="chosen"></div>
  <button type="submit" disabled>{e(t("upload_button", lang))}</button>
  <div class="hint">{e(t("upload_hint", lang))}</div>
</form>
{f'<div class="hint">{e(t("output_note", lang))}</div>' if t("output_note", lang) else ''}
{f'<div class="hint" style="margin-top:10px">{e(notice)}</div>' if notice else ''}
<h2>{e(t("cases", lang))}</h2>{table}"""
    return page(
        t("title", lang), body, refresh=running, lang=lang,
        switch_to=f"/?lang={other}",
    )


def render_triage(record: AuditRecord, lang: str) -> str:
    triage = record.triage
    if not triage:
        return ""
    status = triage.get("status")
    if status == "rejected":
        head = t("triage_rejected", lang)
        detail = (
            f"{e(t('identified_as', lang))}: <b>{e(triage.get('document_type'))}</b> "
            f"({e(t('confidence', lang))} {e(triage.get('confidence'))}). "
            f"{e(t('not_started', lang))}"
        )
    elif status == "unavailable":
        head = t("triage_unavailable", lang)
        detail = e(triage.get("error") or triage.get("reason"))
    else:
        head = t("triage_accepted", lang)
        detail = (
            f"{e(t('identified_as', lang))}: <b>{e(triage.get('document_type'))}</b> "
            f"({e(t('confidence', lang))} {e(triage.get('confidence'))})."
        )
    return f"""<h2>{e(t("triage", lang))}</h2><div class="card">
<div class="rule">{e(head)}</div>
<div class="msg">{detail}</div>
<div class="legal">{e(triage.get('reason'))} · {e(t("model", lang))} <code>{e(triage.get('model'))}</code></div>
</div>"""


def render_findings(record: AuditRecord, lang: str) -> str:
    if not record.findings:
        if record.status == "done":
            return (
                f'<h2>{e(t("findings", lang)).capitalize()}</h2><div class="empty">'
                f'{e(t("no_findings", lang))}</div>'
            )
        return ""
    cards = []
    for finding in record.findings:
        label = e(finding.get("rule_id"))
        if finding.get("apartment_no"):
            label += f", {e(t('apartment', lang))} {e(finding['apartment_no'])}"
        if finding.get("category"):
            label += f", „{e(finding['category'])}”"
        amount = finding.get("amount_involved")
        severity = finding.get("severity", "")
        amount_html = (
            f'<span class="amount {"warn" if severity != "info" else ""}">{money(amount)}</span>'
            if isinstance(amount, (int, float))
            else f'<span class="sev">{e(t("no_amount", lang))}</span>'
        )
        legal = finding.get("legal_reference")
        cards.append(
            f"""<div class="card">
<div class="head"><span class="rule">{label}</span>{amount_html}</div>
<div class="msg">{e(finding.get('message'))}</div>
<div class="sev">{e(t("severity", lang))} {e((SEVERITY_LABELS if lang == "ro" else SEVERITY_LABELS_EN).get(severity, severity))}</div>
{f'<div class="legal">{e(t("basis", lang))}: {e(legal)}</div>' if legal else ''}
</div>"""
        )
    return f'<h2>{e(t("findings", lang)).capitalize()}</h2>' + "".join(cards)


def coverage_status(status: str | None, lang: str) -> str:
    if lang == "ro" or not status:
        return status or ""
    return COVERAGE_STATUS_EN.get(status, status)


def render_coverage(record: AuditRecord, lang: str) -> str:
    coverage = record.coverage_report
    if not coverage:
        return ""
    rules = "".join(
        f"<tr><td><b>{e(rule.get('rule_id'))}</b></td>"
        f"<td>{e(coverage_status(rule.get('status'), lang))}</td>"
        f"<td class='num'>{e(rule.get('checked'))}/{e(rule.get('total'))}</td>"
        f"<td>{e(rule.get('reason') or '')}</td></tr>"
        for rule in coverage.get("rules", [])
    )
    unverified = "".join(
        f"<li><b>{e(item.get('category'))}</b>: {e(item.get('reason'))}</li>"
        for item in coverage.get("expense_lines_unverified", [])
    )
    docs = "".join(
        f"<li>{e(document)}</li>"
        for document in coverage.get("documents_to_request", [])
    )
    return f"""<h2>{e(t("coverage", lang))}</h2>
<div class="card"><div class="rule">{e(coverage.get('summary'))}</div>
{f'<ul class="docs">{unverified}</ul>' if unverified else ''}</div>
<table><tr><th>{e(t("rule", lang))}</th><th>{e(t("col_status", lang))}</th><th class="num">{e(t("checked", lang))}</th><th>{e(t("reason", lang))}</th></tr>
{rules}</table>
{f'<h2>{e(t("documents", lang))}</h2><div class="card"><ul class="docs">{docs}</ul></div>' if docs else ''}"""


def render_timeline(record: AuditRecord, lang: str) -> str:
    steps = []
    for entry in record.step_log:
        if entry.get("status") == "started":
            continue
        css = "failed" if entry.get("status") == "failed" else "ok"
        duration = entry.get("duration_s")
        steps.append(
            f"""<div class="step {css}"><div>{e((STEP_LABELS if lang == "ro" else STEP_LABELS_EN).get(entry['step'], entry['step']))}</div>
<div class="d">{f'{duration:.2f} s' if isinstance(duration, (int, float)) else ''}</div></div>"""
        )
    if not steps:
        return ""
    return f'<h2>{e(t("stages", lang))}</h2><div class="timeline">{"".join(steps)}</div>'


def render_delivery(record: AuditRecord, lang: str) -> str:
    delivery = record.delivery
    if not delivery:
        return ""
    status = delivery.get("status")
    text = {
        "delivered": f"{e(t('delivered_to', lang))} {e(delivery.get('recipient'))}",
        "skipped": t("delivery_off", lang),
        "failed": f"{e(t('delivery_failed', lang))}: {e(delivery.get('error'))}",
    }.get(status, e(status))
    identifier = delivery.get("message_id")
    return f"""<h2>{e(t("delivery", lang))}</h2><div class="card"><div class="msg">{text}</div>
{f'<div class="legal">{e(t("message", lang))} <code>{e(identifier)}</code></div>' if identifier else ''}</div>"""


@app.get("/audit/{audit_id}", response_class=HTMLResponse)
def detail(request: Request, audit_id: str, lang: str = "") -> HTMLResponse:
    lang = pick_language(lang or None, request.headers.get("accept-language"))
    other = "ro" if lang == "en" else "en"
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
        links.append(
            f'<a href="/audit/{e(audit_id)}/letter">{e(t("letter_link", lang))}</a>'
        )
    links += [f"<code>{e(uri)}</code>" for uri in record.artifact_uris]
    artifacts = (
        '<h2>Artefacte</h2><div class="card"><ul class="docs">'
        + "".join(f"<li>{item}</li>" for item in links)
        + "</ul></div>"
    ) if links else ""

    body = f"""<header class="top">
<div class="back"><a href="/?lang={lang}">{e(t("back", lang))}</a></div>
<h1>{e(record.association_ref or record.source_uri.rsplit("/", 1)[-1])}</h1>
<div class="sub">{status_pill(record.status, lang)} &nbsp; <code>{e(record.audit_id)}</code>
&nbsp;·&nbsp; {e(record.document_id or t("no_document", lang))}
&nbsp;·&nbsp; {e(t("period_of", lang))} {e(record.period or t("unknown_org", lang))}
&nbsp;·&nbsp; {when(record.created_at)}</div>
{f'<div class="sub" style="color:var(--warn);margin-top:6px">{e(t("error", lang))}: {e(record.error)}</div>' if record.error else ''}
</header>
{render_triage(record, lang)}
{render_findings(record, lang)}
{render_coverage(record, lang)}
{render_timeline(record, lang)}
{render_delivery(record, lang)}
{artifacts}
<div class="note" style="margin-top:26px">{e(t("source", lang))}: <code>{e(record.source_uri)}</code></div>"""
    return page(
        f"Consilium: {record.audit_id}", body,
        refresh=record.status in RUNNING, lang=lang,
        switch_to=f"/audit/{audit_id}?lang={other}",
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


def safe_name(raw: str) -> str:
    """Nume de obiect previzibil, derivat din ce a încărcat utilizatorul."""
    stem = Path(raw or "document.pdf").name
    cleaned = "".join(
        ch if ch.isalnum() or ch in "._-" else "_" for ch in stem
    ).strip("._")
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return f"{int(time.time())}_{cleaned[:80]}"


def uploads_today(records: list[AuditRecord]) -> int:
    cutoff = time.time() - 24 * 3600
    return sum(1 for record in records if record.created_at >= cutoff)


@app.post("/upload")
async def upload(document: UploadFile = UPLOAD_FIELD) -> Response:
    """Pune documentul în bucket. Restul se întâmplă prin Eventarc, ca de obicei.

    Nu atinge niciun audit existent și nu vorbește cu pipeline-ul: scrie un
    fișier, exact ce ar face `gcloud storage cp`.
    """
    name = (document.filename or "").lower()
    if not name.endswith(".pdf"):
        return RedirectResponse("/?notice=Doar+fișiere+PDF.", status_code=303)

    payload = await document.read()
    if not payload.startswith(b"%PDF"):
        return RedirectResponse(
            "/?notice=Fișierul+nu+pare+a+fi+un+PDF+valid.", status_code=303
        )
    if len(payload) > MAX_UPLOAD_BYTES:
        return RedirectResponse(
            f"/?notice=Fișier+prea+mare+({len(payload) // 1024 // 1024}+MB,+"
            f"maxim+{MAX_UPLOAD_BYTES // 1024 // 1024}+MB).",
            status_code=303,
        )

    try:
        recent = store().list_recent(limit=DAILY_UPLOAD_LIMIT + 5)
    except Exception:  # noqa: BLE001 - un Firestore cazut nu blocheaza incarcarea
        recent = []
    if uploads_today(recent) >= DAILY_UPLOAD_LIMIT:
        return RedirectResponse(
            "/?notice=Plafonul+zilnic+de+documente+a+fost+atins.+"
            "Încearcă+mâine.",
            status_code=303,
        )

    try:
        from google.cloud import storage

        blob = storage.Client().bucket(BUCKET).blob(
            f"{INTAKE_PREFIX}{safe_name(document.filename or '')}"
        )
        blob.upload_from_string(payload, content_type="application/pdf")
    except Exception as error:  # noqa: BLE001
        return RedirectResponse(
            f"/?notice=Încărcare+eșuată:+{e(error)[:120]}", status_code=303
        )

    return RedirectResponse(
        "/?notice=Document+încărcat.+Auditul+pornește+în+câteva+secunde.",
        status_code=303,
    )

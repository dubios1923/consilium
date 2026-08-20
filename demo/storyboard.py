"""Scenariul prezentării: ce se vede, ce se spune, cât durează.

Un singur loc care ține și textul narațiunii, și acțiunea vizuală. Durata unei
scene nu se scrie de mână: o dă lungimea audio-ului generat, iar video-ul se
întinde sau se comprimă ca să se potrivească. Așa nu se desincronizează nimic
când se schimbă o frază.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Action = Literal[
    "card", "detail", "letter", "upload", "wait_state", "terminal"
]
# `upload` incarca prin pagina publica, exact cum ar face un proprietar.


@dataclass
class Scene:
    """O scenă: o replică de narațiune peste o singură acțiune vizuală."""

    key: str
    narration: str
    action: Action
    card_title: str = ""
    card_lines: list[str] = field(default_factory=list)
    # pentru `upload` si `wait_state`
    sample: str = ""
    target_status: str = ""
    # cat sa astepte minim, chiar daca naratiunea e mai scurta
    min_seconds: float = 0.0
    # pentru `detail` si `letter`: pe ce dosar
    audit_key: str = ""
    # derulare lenta in pagina, ca sa se vada continutul
    scroll_to: int = 0
    # pentru `terminal`: comenzi rulate chiar atunci, cu output-ul lor real
    commands: list[list[str]] = field(default_factory=list)


STORYBOARD: list[Scene] = [
    Scene(
        key="problem",
        action="card",
        card_title="The document nobody checks",
        card_lines=[
            "A Romanian homeowners' association posts a monthly payment list.",
            "28 apartments, 15 columns, eight different allocation keys.",
            "Owners have 10 days to contest how their share was calculated.",
            "Almost nobody does. Checking it means knowing the law and the arithmetic.",
        ],
        narration=(
            "Every month, a Romanian homeowners' association posts a payment list "
            "on the notice board. Twenty-eight apartments, eight different "
            "allocation keys. Owners have ten days to contest their share. Almost "
            "nobody does: checking it means knowing both the statute and the "
            "arithmetic."
        ),
    ),
    Scene(
        key="thesis",
        action="card",
        card_title="Consilium",
        card_lines=[
            "Drop a PDF in a bucket.",
            "Get back the findings, what could not be verified,",
            "and the formal contestation letter, on the correct legal basis.",
            "No human in the loop.",
        ],
        narration=(
            "Consilium audits that document. You drop a PDF into a bucket and you "
            "get back the findings, an explicit report of what could not be "
            "verified, and a formal contestation letter on the correct legal "
            "basis. Nothing in between needs a human."
        ),
    ),
    Scene(
        key="gate_upload",
        action="upload",
        sample="sample_not_a_payment_list.pdf",
        narration=(
            "Here is the whole interface. You pick a file and press one button. "
            "First, something that is not a payment list: a general assembly "
            "resolution."
        ),
        min_seconds=8.0,
    ),
    Scene(
        key="gate_result",
        action="wait_state",
        target_status="rejected",
        narration=(
            "A small model looks at the first page only and rejects it in about "
            "four seconds. Extraction costs eighty-two seconds of Gemini, so the "
            "cheapest question is asked first. The gate fails open: if it cannot "
            "decide, the document goes through anyway."
        ),
        min_seconds=6.0,
    ),
    Scene(
        key="real_upload",
        action="upload",
        sample="sample_errors.pdf",
        narration="Now a real payment list, the same way.",
        min_seconds=6.0,
    ),
    Scene(
        key="pipeline",
        action="wait_state",
        target_status="done",
        narration=(
            "The pipeline runs detached in a Cloud Run job, triggered by Eventarc. "
            "Six stages, each one writing its state to Firestore as it goes. "
            "Transcription, then an arithmetic integrity check, then the "
            "deterministic rules, then the letter, then delivery. The page you are "
            "watching is read only. It polls; it cannot start anything."
        ),
        min_seconds=8.0,
    ),
    Scene(
        key="findings",
        action="detail",
        audit_key="real",
        narration=(
            "Four findings. The declared grand total is nine hundred lei above the "
            "sum of its own expense lines. Heating is declared as allocated by "
            "undivided ownership share, but the per apartment amounts follow hot "
            "water consumption instead: three thousand three hundred lei "
            "redistributed against the declared key. Every figure here was "
            "computed by ordinary Python, not by a model."
        ),
        min_seconds=10.0,
    ),
    Scene(
        key="coverage",
        action="detail",
        audit_key="real",
        scroll_to=1100,
        narration=(
            "And this is the part most tools leave out. Six of eight expense lines "
            "verified. The other two cannot be checked at all, because the "
            "document does not publish individual meter readings. That gap is a "
            "result, not a silence: it tells the owner exactly which document to "
            "ask for next."
        ),
        min_seconds=9.0,
    ),
    Scene(
        key="letter",
        action="letter",
        audit_key="real",
        narration=(
            "The letter is generated from those findings and then verified against "
            "them. Every amount in it has to come from a computed finding, and "
            "every paragraph has to point at one. A letter that fails the check is "
            "not sent. It also picks the right legal ground: article twenty eight "
            "has three separate paragraphs, with different deadlines, and the "
            "system checks whether the contestation window is still open before "
            "deciding which document it is writing."
        ),
        min_seconds=12.0,
    ),
    Scene(
        key="finding_r0",
        action="card",
        card_title="Self-reported confidence does not detect reading errors",
        card_lines=[
            "On a degraded scan: 99.61% transcription fidelity.",
            "Both errors flagged as low confidence: zero.",
            "Apartment 7, hot water: 41,80 read as 34,20.",
            "R0 caught it because the row stopped summing to its printed total.",
            "Re-read disagreed. Cell marked unauditable. Zero false findings.",
        ],
        narration=(
            "Two things we learned. First: asking a model how confident it is "
            "does not detect its own reading errors. On a degraded scan, fidelity "
            "was ninety nine point six percent, and both errors were reported with "
            "full confidence. The fix is arithmetic redundancy: rows sum to totals, "
            "columns sum to declared amounts. When apartment seven stopped adding "
            "up, the cell was marked unauditable instead of becoming an accusation."
        ),
        min_seconds=14.0,
    ),
    Scene(
        key="finding_baseline",
        action="card",
        card_title="Why not just a chatbot?",
        card_lines=[
            "Same model, same PDFs, one call, the prompt a person would type.",
            "Identifies well: 2.8 of 3, and 3.4 of 4.",
            "Quantifies badly: 0.2 of 4.",
            "On a correct document it invented 2.4 findings per run.",
            "It misread 347,4 as 347,2, did correct arithmetic on its own",
            "wrong input, and accused the association.",
        ],
        narration=(
            "Second: we measured the obvious objection. Same model, same PDFs, one "
            "call, the prompt a person would actually type. It finds things well. "
            "It quantifies badly: zero point two out of four. And on a correct "
            "document it invented findings in every run. In one, it misread a "
            "digit, did perfectly correct arithmetic on its own wrong input, and "
            "accused the association. That is the failure the integrity check "
            "exists for."
        ),
        min_seconds=16.0,
    ),
    Scene(
        key="proof",
        action="terminal",
        commands=[
            # Serviciile intai: URL-ul .run.app e dovada ceruta explicit de
            # regulament ca backendul ruleaza pe Google Cloud, iar captura
            # headless nu are bara de adrese unde sa se vada altfel.
            ["gcloud", "run", "services", "list", "--region=europe-west1",
             "--format=table(metadata.name,status.url)"],
            # Doar executiile incheiate: o coloana goala pentru una inca in curs
            # se citeste gresit ca esec.
            ["gcloud", "run", "jobs", "executions", "list",
             "--job=consilium-audit", "--region=europe-west1", "--limit=4",
             "--filter=status.succeededCount>0",
             "--format=table(metadata.name,status.succeededCount)"],
            ["gcloud", "storage", "ls", "gs://consilium-intake-ab7x21/output/"],
        ],
        narration=(
            "None of that was staged. Both services run on Cloud Run, each upload "
            "triggered a job execution through Eventarc, and each finished audit "
            "wrote its letter, its findings and its report into the bucket under "
            "its own case folder."
        ),
        min_seconds=15.0,
    ),
    Scene(
        key="architecture",
        action="card",
        card_title="Architecture",
        card_lines=[
            "GCS -> Eventarc -> Cloud Run Job -> ADK SequentialAgent",
            "Triage and Extractor touch a model. Integrity and Reconciler do not.",
            "The audit rules are ordinary Python, verified by an AST test",
            "that no model SDK is imported.",
            "274 tests. Firestore state. Secrets in Secret Manager.",
        ],
        narration=(
            "The architecture puts the boundary in one place. Triage and "
            "extraction touch a model. The integrity check and the audit rules do "
            "not, and a test enforces that by parsing the source and asserting no "
            "model SDK is imported. Two hundred and seventy four tests, state in "
            "Firestore, secrets in Secret Manager."
        ),
        min_seconds=12.0,
    ),
    Scene(
        key="close",
        action="card",
        card_title="Consilium",
        card_lines=[
            "Romanian homeowners' associations are the first vertical.",
            "The shape generalises: utility bills, medical bills,",
            "insurance settlements, supplier invoices to small businesses.",
            "A financial document from a party with an interest in erring",
            "in its own favour, and a deadline to object.",
        ],
        narration=(
            "Homeowners' associations are the first vertical because it is the "
            "problem I have. The shape is general: a financial document from a "
            "party with an interest in erring in its own favour, and a deadline to "
            "object. The rules change. The integrity check does not."
        ),
        min_seconds=12.0,
    ),
]

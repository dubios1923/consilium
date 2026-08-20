# Consilium

**Track: The Taskmaster**

Live page: https://consilium-dashboard-aq2ftfgfkq-ew.a.run.app

---

## The problem

Every month a Romanian homeowners' association posts a payment list on the
notice board. Twenty-eight apartments, fifteen columns, eight different
allocation keys: water by metered consumption, heating by undivided ownership
share, sanitation by headcount, administration split equally. Owners have ten
days from that posting to contest how their share was calculated, and the
association president is then obliged to answer in writing within another ten.

Almost nobody contests anything, because checking the document means knowing
both the statute and the arithmetic. The deadline passes, the right lapses, and
next month starts over.

## What it does

You copy a PDF into a bucket. Nothing else. Roughly two minutes later there is a
formal contestation letter, a machine readable findings file, and an audit
report, written to Cloud Storage and emailed as an attachment.

**Features**

- **Entry triage.** A small model reads only the first page and decides whether
  the document is a payment list at all. A general assembly resolution is
  rejected in about four seconds without spending the eighty-two seconds that
  extraction costs. The gate fails open: if it cannot decide, the document goes
  through anyway.
- **Transcription with no arithmetic.** The extractor copies printed values into
  a validated Pydantic structure across three passes. It is forbidden to compute
  anything. A field it cannot read goes into `low_confidence_fields` rather than
  being guessed.
- **An integrity check before any audit.** R0 verifies that the transcription
  adds up against itself: every row sums to its printed total, every column sums
  to its declared category amount. When a row and a column fail by the same
  amount, their intersection localises a single cell and one targeted re-read is
  spent on it.
- **Seven deterministic audit rules.** Declared totals, undivided shares summing
  to 100%, each expense allocated by its declared key, late payment penalties
  against the 0.2% per day legal cap (art. 77 alin. (2) of Law 196/2018),
  current month charges against the grand total, apartment count, and whether
  the contestation window is still open.
- **A coverage report.** What could not be verified and which document would
  settle it. On our error sample this is six of eight expense lines checked and
  4.749,95 lei left unauditable, because the association did not publish
  individual meter readings.
- **A letter that cannot lie.** Generated from the findings and then verified
  against them: every amount must come from a computed finding, every paragraph
  must point at one. A letter that fails the check is never sent.
- **The right legal ground.** Art. 28 has three separate paragraphs with
  different regimes. If the ten day window is open the system writes a
  contestation under alin. (3); if it has expired it writes a request for copies
  under alin. (1), which has no deadline, and says so.
- **Email delivery** as an optional final step that cannot fail an audit.
- **A read only inspection page** that shows every case, its stage timings, its
  findings and its letter.

## Tech stack

- **Gemini 3.5 Flash** on Vertex AI (`location=global`) for transcription and
  for drafting the letter. **Gemini 2.5 Flash-Lite** for the triage gate.
- **Google ADK**: the pipeline is a `SequentialAgent` with six sub-agents.
  Triage, Extractor, Integrity, Reconciler, Drafter, Delivery.
- **Google Cloud**: Cloud Storage for intake and artifacts, Eventarc for the
  trigger, a Cloud Run Job for the pipeline, two Cloud Run services (an event
  launcher and the inspection page), Firestore for audit state, Secret Manager
  for the delivery key, Cloud Build and Artifact Registry for deployment, and
  Cloud Text-to-Speech for the demo narration.

Four of the six sub-agents are `BaseAgent` subclasses running ordinary Python
rather than `LlmAgent`. Only triage, extraction and letter drafting touch a
model. The audit rules are subtractions and comparisons against a tolerance; a
model executing them would add a class of error in exchange for no capability. A
test parses `reconciler.py` and asserts that no model SDK appears among its
imports.

## Data sources

There is no public corpus of Romanian payment lists, and real ones carry names,
addresses and tax IDs. So `tools/gen_samples.py` generates them: deterministic
from a seed, entirely fictitious, with every planted error documented in
`expected_findings.json` by id, type, location, correct value against found
value, and severity.

It emits three documents (clean, arithmetic errors, penalty violations) in three
forms each: a native PDF, a second layout with different headers, reordered
columns and abbreviations, and a degraded scan at 150 dpi with random skew,
Gaussian noise and JPEG compression. Plus a non-payment-list document for the
triage gate. Same seed, same MD5.

The legal thresholds live in `config.yaml` rather than in code, because the
article references and deadlines are the part that does not transfer to another
jurisdiction.

## Findings

**Self-reported confidence does not detect reading errors.** On the degraded
scans the extractor achieved 99.610%, 99.781% and 100.000% transcription
fidelity, and reported **zero** low confidence fields. All three errors passed
silently. The worst was apartment 7's hot water charge, 41,80 lei read as 34,20,
which is the value from another row. Asking a model to introspect on its own
certainty is not a usable detector. Arithmetic redundancy is: R0 caught two of
two silent financial errors, and produced zero false audit findings on the scans.

**We measured the obvious objection.** `tools/baseline_chat.py` sends the same
PDFs to the same model in one call with the prompt a person would type, five
runs each. It identifies well: 2.8 of 3 and 3.4 of 4 planted findings. It
quantifies badly: 0.2 of 4. It reports the penalty printed in the document and
almost never the excess over the legal cap, which is the number needed to file
anything. On a document with no planted errors it invented 2.4 findings per run.
In one, it misread 347,4 as 347,2, did perfectly correct arithmetic on its own
wrong input, and accused the association of understating a meter reading.

**A strict validator that fails closed tells you where your own definition is
wrong.** The letter validator rejected three drafts in a row over two amounts.
The model had invented nothing: the figures came from the reconciler's own
explanation text, which our allowlist did not scan. The validator was right to
reject and wrong about provenance.

**Transcription generalises further than the deterministic fallback does.** The
second layout is read at 100.000% fidelity with no code changes, but the
abbreviated column headers are matched by the model, which receives the category
list from the first pass. Fed those abbreviations directly, the deterministic
aligner resolves two of eight and reports the rest rather than guessing.

## Status

Deployed and running. 257 tests, all offline, none of which calls a model.
Uploading a file to the intake bucket triggers a real Cloud Run job execution
through Eventarc, and the demo video shows that happening rather than asserting
it.

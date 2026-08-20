## Inspiration

Every month the administrator of a Romanian homeowners' association posts a
payment list on the notice board. Twenty-eight apartments, fifteen columns,
eight different allocation keys: water by metered consumption, heating by
undivided ownership share, sanitation by headcount, administration split
equally.

By law the owner has ten days from that posting to contest how their share was
calculated, and the association president must then answer in writing within
another ten. In practice almost nobody contests anything, because checking the
document means knowing both the statute and the arithmetic. The deadline passes,
the right lapses, and next month starts over.

I wanted to know whether that document could be checked automatically, and what
it would take for the answer to be trustworthy enough to put in a letter.

## What it does

You drop a PDF into a bucket, or now just pick it on a web page. Roughly two
minutes later there is a formal contestation letter, a machine readable findings
file and an audit report, written to Cloud Storage and emailed as an attachment.

- **An entry gate** reads only the first page and decides whether the document
  is a payment list at all. A general assembly resolution is rejected in about
  four seconds instead of spending the eighty-two seconds that extraction costs.
  The gate fails open: if it cannot decide, the document goes through anyway.
- **Transcription with no arithmetic.** The extractor copies printed values into
  a validated structure across three passes and is forbidden to compute
  anything. A field it cannot read is flagged, never guessed.
- **An integrity check before any audit.** Every row must sum to its printed
  total and every column to its declared category amount. When a row and a
  column fail by the same amount, their intersection localises one cell and a
  single targeted re-read is spent on it.
- **Seven deterministic rules.** Declared totals, undivided shares summing to
  100%, each expense allocated by its declared key, penalties against the 0.2%
  per day legal cap, current month charges against the grand total, apartment
  count, and whether the contestation window is still open.
- **A coverage report.** What could not be verified and which document would
  settle it. On the error sample that is six of eight expense lines checked and
  4.749,95 lei left unauditable, because the association did not publish meter
  readings.
- **A letter that cannot lie.** Generated from the findings and then verified
  against them: every amount must come from a computed finding and every
  paragraph must point at one. A letter that fails the check is never sent.
- **The correct legal ground.** Article 28 has three separate paragraphs with
  different regimes. If the ten day window is open the system writes a
  contestation under paragraph 3; if it has expired it writes a request for
  copies under paragraph 1, which carries no deadline, and says so.

## How I built it

The pipeline is a Google ADK `SequentialAgent` with six sub-agents: Triage,
Extractor, Integrity, Reconciler, Drafter, Delivery. Four of them are
`BaseAgent` subclasses running ordinary Python rather than `LlmAgent`. Only
triage, extraction and letter drafting touch a model.

That split is the central design decision. The audit rules are subtractions and
comparisons against a tolerance. A model executing them would introduce a class
of error that otherwise does not exist, in exchange for no capability at all,
and the output of this system is an accusation somebody will contest. A test
parses `reconciler.py` and asserts that no model SDK appears among its imports,
so the boundary is enforced structurally rather than by convention.

Everything runs on Google Cloud: an object landing in Cloud Storage triggers
Eventarc, which starts a Cloud Run Job through a small launcher service. State
goes to Firestore at every transition. The delivery key lives in Secret Manager.
Gemini 3.5 Flash on Vertex AI does transcription and drafting; Gemini 2.5
Flash-Lite runs the gate. A read-only Cloud Run service shows every case with
its stage timings, findings, coverage report and letter.

**Data sources.** There is no public corpus of Romanian payment lists, and real
ones carry names, addresses and tax IDs. So the generator produces them:
deterministic from a seed, entirely fictitious, with every planted error
documented by id, type, location, correct value against found value, and
severity. Three documents in three forms each, a native PDF, a second layout
with different headers and abbreviations, and a degraded 150 dpi scan with skew,
noise and JPEG compression. Same seed, same MD5.

Even the demo video is generated rather than filmed. A storyboard holds
narration and visual action together, Playwright drives the browser and captures
lossless frames, and ffmpeg fits each scene to the length of its line. Two more
Google models do the audio: Cloud Text-to-Speech produces the narration, and
Lyria writes the score, which is sidechained to the voice so it steps aside
whenever somebody is speaking. Veo and Gemma were both tried and are not
callable in this project, which is documented rather than worked around.

## Challenges I ran into

**A model's confidence tells you nothing about its reading.** On the degraded
scans the extractor reached 99.610%, 99.781% and 100.000% fidelity and reported
zero low confidence fields. All three errors passed silently. The worst was one
apartment's hot water charge, 41,80 lei read as 34,20, which is the value from
another row. Asking a model to introspect is not a detector. Arithmetic
redundancy is, which is where R0 came from.

**A strict validator that fails closed will find your own mistakes.** The letter
validator rejected three drafts in a row over two amounts. The model had
invented nothing: the figures came from the reconciler's own explanation text,
which my allowlist did not scan. The validator was right to reject and wrong
about provenance.

**Deployment details that cost hours.** Eventarc cannot target a Cloud Run Job
directly in current gcloud, so a launcher service sits in between. The Eventarc
service agent does not exist on first use and its permissions take minutes to
propagate. `gcloud storage service-agent` returns its value with a leading
newline, which produces an empty IAM member and a confusing error.

**A bug that only existed in production.** The letter PDF was rendered entirely
in Helvetica, with every Romanian diacritic a black box, because font lookup
used hardcoded Fedora paths while the container is Debian. The fallback returned
Helvetica silently, so it looked fine locally. There is no fallback now: without
a suitable font the drafter raises rather than producing an unreadable document.

## Accomplishments that I'm proud of

I measured the obvious objection instead of arguing about it. A benchmark sends
the same PDFs to the same model in one call with the prompt a person would type,
five runs each. It identifies well, 2.8 of 3 and 3.4 of 4 planted findings. It
quantifies badly, 0.2 of 4: it reports the penalty printed in the document and
almost never the excess over the legal cap, which is the number you need to file
anything. On a document with no planted errors it invented 2.4 findings per run.
In one run it misread 347,4 as 347,2, did perfectly correct arithmetic on its
own wrong input, and accused the association of understating a meter reading.

On the same scan where my own extractor also misread a cell, the integrity check
caught it because the row stopped summing, the re-read disagreed, and the cell
was marked unauditable instead of becoming an accusation. Zero false audit
findings.

267 tests, all offline, none of which calls a model.

## What I learned

The interesting engineering was not in the prompts. It was in deciding what the
model is allowed to be wrong about, and building a check that does not depend on
the model being honest with itself.

I also learned to distrust a clean result. Every number in the README that
sounded good turned out to need a caveat once I looked: the second layout is
read at 100% fidelity, but the abbreviations are matched by the model, and the
deterministic fallback behind it resolves only two of eight.

## What's next for Consilium

Romanian homeowners' associations are the first vertical because it is the
problem I have. The shape is general: a financial document issued by a party
with an interest in erring in its own favour, sent to someone with a deadline to
object and no practical way to check. Utility bills, medical bills, insurance
settlements, supplier invoices to small businesses. The schema and the rules
change per vertical. The integrity check, the model/deterministic split, the
claim validator and the coverage report do not.

The nearest missing piece is reconciliation against supplier invoices. Today the
system verifies only the internal coherence of the list, so an inflated invoice
that is allocated correctly balances perfectly and produces nothing. That is why
the letter always requests the invoices.

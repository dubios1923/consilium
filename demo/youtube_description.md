# YouTube

**Title**

    Consilium: an agent that audits a payment list and files the contestation

**Visibility: Public.** The hackathon rules require the video to be publicly
visible. Unlisted does not satisfy that.

**Description** (copy everything below the line)

---

Consilium audits the monthly payment list a Romanian homeowners' association
posts on the notice board, then drafts the formal contestation the owner is
legally entitled to file. Built for the All Things Agentic Hackathon, track The
Taskmaster.

You drop a PDF into a bucket, or pick it on a web page. Two minutes later there
is a contestation letter, a machine-readable findings file and an audit report
in Cloud Storage, emailed as an attachment.

The design decision the project is built around: only three of the six
sub-agents touch a model. Triage reads the first page to decide whether the
document is worth the expensive pipeline. Extraction transcribes and is
forbidden to compute anything. Drafting writes the letter, which is then
verified against the findings. The integrity check and the seven audit rules
are ordinary Python, and a test parses the source to prove no model SDK is
imported there.

Two measured results:

Asking a model how confident it is does not detect its own reading errors. On
degraded scans the extractor reached 99.61% transcription fidelity and reported
zero low-confidence fields, while getting two cells wrong. One read 41,80 lei
as 34,20, the value from another apartment's row, picked up from page skew.
What works instead is redundancy the document already contains: rows sum to
totals, columns sum to declared amounts, and a row and a column failing by the
same amount localise a single cell.

The same model in a chat window, one call, five runs per document: it identifies
3.4 of 4 planted findings but quantifies 0.2 of 4, and on a document with no
errors at all it invented 2.4 findings per run. In one run it misread a digit,
did perfectly correct arithmetic on its own wrong input, and accused the
association.

Chapters:
0:00 The document and the ten-day deadline
0:16 What the system does
0:30 The entry gate rejects a non-payment-list
0:53 A real upload from the browser
1:17 Findings and the coverage report
1:55 The letter, and the legal ground it cites
2:20 Why self-reported confidence is not a detector
2:44 The same model in a chat, measured
3:10 Deployed, with real commands
3:25 Architecture and what generalises

Everything on screen is real output. The sample documents are entirely
fictitious, generated deterministically from a seed with every planted error
documented in advance.

Built with: Google ADK, Gemini 3.5 Flash and Gemini 2.5 Flash-Lite on Vertex AI,
Cloud Run, Cloud Run Jobs, Eventarc, Cloud Storage, Firestore, Secret Manager,
Cloud Build, Artifact Registry, Cloud Text-to-Speech, Lyria, Python.

The video itself is generated rather than filmed: a storyboard holds narration
and visual action together, Playwright captures the frames, Cloud
Text-to-Speech reads the script and Lyria writes the score.

Code: https://github.com/dubios1923/consilium
Live inspection page: https://consilium-dashboard-aq2ftfgfkq-ew.a.run.app

#AllThingsAgenticHackathon

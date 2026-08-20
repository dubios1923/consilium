# Social posts

Both carry the required hashtag. Post either or both; each is written to stand
on its own.

---

## LinkedIn

I spent this month building an agent that audits the monthly payment list a
Romanian homeowners' association posts on the notice board. Twenty-eight
apartments, fifteen columns, eight allocation keys, ten days to contest it by
law, and almost nobody ever does.

The part I did not expect was this.

I fed the extractor a deliberately degraded scan. It achieved 99.61%
transcription fidelity and reported zero low-confidence fields. It also got two
cells wrong. One of them read 41,80 lei as 34,20, which is the value from
another apartment's row, picked up because the page was slightly skewed.

The model did not perceive that cell as illegible. It perceived it as legible,
and was wrong, with full confidence. Asking a model how sure it is does not
detect its own reading errors, and no prompt fixes that, because it has no
independent check on itself.

What works is redundancy the document already contains. Rows sum to totals.
Columns sum to declared amounts. A deterministic check runs before any audit
rule and verifies the transcription against itself. When a row and a column fail
by the same amount, their intersection points at one cell, and a single targeted
re-read is spent on it.

The re-read returned the correct value. The system threw it away anyway. Two
readings that disagree mean you do not know which is right, so the cell is
marked unauditable rather than becoming an accusation. Zero false findings on
that document.

I also measured the obvious objection: same model, same PDF, one call, the
prompt a person would type. It finds things well. It quantifies badly, 0.2 of 4.
And on a document with no errors at all it invented findings in every run. In
one, it misread a digit, did perfectly correct arithmetic on its own wrong
input, and accused the association of understating a meter reading.

That is why the audit rules in my system are ordinary Python, and a test parses
the source to prove no model SDK is imported there.

Built on Google ADK, Gemini 3.5 Flash on Vertex AI, Cloud Run, Eventarc and
Firestore. Code and architecture in the repo.

#AllThingsAgenticHackathon

---

## X

Built an agent that audits Romanian HOA payment lists.

Fed it a degraded scan. 99.61% fidelity, zero low-confidence flags, and two
cells wrong. One read 41,80 as 34,20: the value from another row, picked up
from page skew.

Asking a model how confident it is does not detect its own reading errors.

What works: the document already contains its own check. Rows sum to totals,
columns sum to declared amounts. A deterministic pass verifies the transcription
against itself before any audit rule runs. Row and column failing by the same
amount localises one cell.

The re-read got it right. The system discarded it anyway, because two readings
that disagree mean you do not know which is correct. Cell marked unauditable
instead of becoming an accusation.

Same model in a chat, one call, five runs: identifies 3.4 of 4 findings,
quantifies 0.2 of 4, and invents 2.4 findings on a document with none. In one
run it misread a digit, computed correctly on its own wrong input, and accused
the association.

Google ADK, Gemini 3.5 Flash on Vertex AI, Cloud Run, Eventarc, Firestore.

#AllThingsAgenticHackathon

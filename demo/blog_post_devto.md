---
title: Asking a model how confident it is does not detect its own reading errors
published: true
description: I built an agent that audits Romanian homeowners' association payment lists. The extractor hit 99.61% fidelity on a degraded scan, flagged nothing, and got two cells wrong.
tags: googlecloud, ai, agents, python
cover_image: https://raw.githubusercontent.com/dubios1923/consilium/master/demo/gallery/architecture.png
---

*I wrote this post for the purposes of entering the All Things Agentic
Hackathon, about the agent I built for it. Here is the part that surprised me.*

---

Every month the administrator of a Romanian homeowners' association posts a
payment list on the notice board. Twenty-eight apartments, fifteen columns,
eight different allocation keys. By law you have ten days to contest how your
share was calculated. Almost nobody does, because checking it means knowing both
the statute and the arithmetic.

I built an agent that does the checking. The interesting part was not the
prompts.

## The instruction that does not work

The extractor has one job: copy printed values out of a PDF into a validated
structure. It is explicitly forbidden to compute anything. And it is told, in
capital letters, that a value it cannot read clearly goes into
`low_confidence_fields` rather than being guessed.

To test whether that instruction held, I made the generator produce degraded
versions of the sample documents: rasterised to 150 dpi, skewed by up to 1.5
degrees, Gaussian noise, JPEG at quality 75, repackaged as a PDF with no text
layer. `pdftotext` returns three bytes. The model has to actually read pixels.

The result:

| document | fields | errors | fidelity | `low_confidence_fields` |
|---|---|---|---|---|
| clean scan | 513 | 2 | 99.610% | **0** |
| errors scan | 457 | 1 | 99.781% | **0** |
| penalties scan | 513 | 0 | 100.000% | 0 |

Three errors. Zero flagged.

The worst one: apartment 7's hot water charge, `41,80` lei, read as `34,20`.
That is not a mangled digit. It is the value from apartment 5's row, picked up
because the page was skewed. The model did not perceive the cell as illegible.
It perceived it as legible, and was wrong, with full confidence.

This matters more than a fidelity number suggests. The row's printed total was
still transcribed correctly, so the breakdown no longer summed to it. Downstream,
that looks exactly like an association misallocating money. An audit built on
that produces a formal accusation from a scanning artefact.

**A model's introspection about its own certainty is not a detector.** No amount
of prompt engineering fixes this, because the model has no independent check on
its own reading.

## What works instead

A financial document contains its own redundancy. Rows sum to totals. Columns
sum to declared amounts. That property has nothing to do with heating or
undivided ownership shares; it holds because somebody laid the document out
expecting the numbers to add up.

So before any audit rule runs, a deterministic check called R0 verifies the
transcription against itself:

- per row: Σ charges + arrears + penalties == the printed total due
- per column: Σ across apartments == the category's declared amount

On the clean scan, both fired:

```
[R0.row]    Apartamentul 7: suma defalcării (891.33) nu dă totalul de plată
            tipărit (898.93); diferență -7.60 lei.
[R0.column] Coloana „Apă caldă menajeră": suma pe apartamente (3218.60) nu dă
            suma declarată în tabelul de cheltuieli (3226.20); diferență -7.60 lei.
```

Same difference, both times. When a row and a column fail by the same amount,
their intersection localises a single cell. One targeted re-read is spent on it:
that page rendered at 300 dpi, with the response narrowed to one cell.

The second reading returned `41,80`. The correct value.

And then the system threw it away.

## The conservative rule

Two readings that disagree mean you do not know which one is right. Adopting the
second because it happens to balance is just preferring the answer you like. So
apartment 7 is marked **unauditable**, the cell goes into
`low_confidence_fields`, and the rule that depends on it reports partial
coverage instead of a finding.

The result on that document: **zero false audit findings**. Not "we caught the
error", which would still leave the question of what else we missed. Zero
accusations generated from a scanning artefact.

Field-level precision matters here. The uncertain value is one charge cell, so
the rules that never touch it still run: the undivided shares still sum to 100%,
the penalties are still checked against the legal cap, the apartment count is
still verified. One bad cell does not void an audit.

## The comparison worth making

The obvious objection is that a good model in a chat window would do this
anyway. So I measured it: same model, same PDFs, one call, the prompt a person
would actually type. *"Check this payment list, find the errors, and draft a
formal request to the association."* Five runs per document.

It identifies well. 2.8 of 3 and 3.4 of 4 planted findings on average, and four
of five runs quoted all three abusive penalty amounts correctly.

It quantifies badly. **0.2 of 4.** It reports the penalty printed in the
document and almost never the excess over the legal cap, which requires knowing
the cap, applying it to the arrears, and subtracting. The first number the owner
can read themselves. The second is the one they need to file anything.

And on a document with **zero** planted errors, every run reported problems
anyway. From one of them, verbatim:

```
#### 1. Eroare de calcul la consumul de apă (Ap. 3)
*   Index vechi:      347,2 mc
*   Index nou:        353,0 mc
*   Consum real:      353,0 - 347,2 = 5,8 mc
*   Consum înregistrat în tabel: 5,6 mc (eroare de 0,2 mc în minus...)
```

The document says **347,4**, not 347,2. The model misread one digit, did
perfectly correct arithmetic on its own wrong input, and concluded that the
association had understated a meter reading. Everything downstream of that is
built on a digit that was never there.

That is the same failure as apartment 7. The difference is that one system has a
check that does not depend on the model being honest with itself, and the other
does not.

## The boundary

This is why the audit rules are ordinary Python and not an agent.

The pipeline is a Google ADK `SequentialAgent` with six sub-agents. Only three
touch a model: triage reads the first page to decide whether the document is
worth the expensive pipeline at all, extraction transcribes, and drafting writes
the letter. The integrity check and the seven audit rules are subtractions and
comparisons against a tolerance. A model executing them would introduce a class
of error that otherwise does not exist, in exchange for no capability.

A test parses `reconciler.py` and asserts that no model SDK appears among its
imports, so the boundary is enforced structurally rather than by convention.

The same reasoning shapes the letter. It is generated by a model and then
verified against the findings: every amount in it must come from a computed
finding, and every paragraph must point at one. A letter that fails the check is
never sent.

That validator caught something too, though not what I expected. It rejected
three drafts in a row over two amounts. The model had invented nothing: the
figures came from the reconciler's own explanation text, which my allowlist did
not scan. The validator was right to reject and wrong about provenance. A strict
check that fails closed shows you where your own definition is incomplete,
instead of letting the output through.

## What generalises

Consilium audits Romanian homeowners' association payment lists because that is
the problem I have. But the shape is not specific to it: a financial document
issued by a party with an interest in erring in its own favour, sent to someone
with a legal deadline to object and no practical way to check. Utility bills.
Medical bills. Insurance settlements. Supplier invoices to small businesses.

The schema changes. The rules change. The article numbers change.

R0 does not, because it encodes no domain knowledge at all. It only requires
that the numbers were supposed to add up.

---

*Code, architecture diagram and reproducible setup:
https://github.com/dubios1923/consilium*

*Live inspection page:
https://consilium-dashboard-aq2ftfgfkq-ew.a.run.app*

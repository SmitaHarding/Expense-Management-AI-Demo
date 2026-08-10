# Evaluation — how we know it works

A design like this makes testable claims: that two independent readers catch errors a single
reader wouldn't, that the deterministic policy engine routes exactly as specified, and that when
the two readers *agree* they're actually right. This page summarizes how those claims were measured.

The evaluation runs on two layers: a **deterministic conformance suite** (does the policy/routing
engine do what the spec says, every time?) and a **real-receipt accuracy measurement** (how well
do the readers actually read?). The corpus here is small — tens of receipts, not thousands — so
every real-accuracy number below is **provisional**, an honest first read rather than a statistical
guarantee. A production pilot would re-measure on a much larger corpus. That caveat is the point:
the system is built so that when it's *uncertain*, it routes to a human — so being provisional is
safe, not dangerous.

*Measured on `expense-ai-demo` @ `8f8387c`, policy config 0.9.6, 2026-08-10 — one full
round: the deterministic conformance suite plus real Claude vision on all four corpora
(synthetic, domestic, EUR, srd_usd).*

---

## Track A — pipeline conformance (deterministic)

**18 / 18 scenarios pass.** A hand-built suite of expense reports, each exercising one specific rule,
run through the *real* policy and routing engine and checked against the expected route and flags.
It needs no API key and no receipt images — it's fully deterministic and reproducible.

What it proves, concretely: a clean small report auto-approves; a clean large report routes to a
manager as *routine sign-off*, not a breach; an over-cap hotel escalates to manager **then** the
travel approver; a report whose two readers disagree routes to a human; an accommodation folio's
per-night cost is computed from the nights read off the folio, and a disagreement on those nights
routes to review; a human correction re-runs every policy check but can never erase the original
machine disagreement. Each of these is a named test with an expected outcome — not a vibe.

---

## Track B — extraction accuracy on real receipts (n = 31 domestic)

Two readers read the same real receipt images independently. Measured field-by-field:

| Field | OCR (tesseract) | Vision model | 
|---|---|---|
| Amount | 81% | **94%** |
| Date | 74% | **90%** |
| Merchant | 58% | **90%** |

**Why this shapes the design.** The vision model is clearly the stronger reader, and OCR is the
cross-check — not a co-equal extractor. That's exactly why the system uses the *agreement* of the two
readers as the gate for automation, rather than trusting either one alone. A receipt only qualifies
for automatic handling when both readers land on the same amount and date; any disagreement is
treated as a signal and routed to a person.

---

## The metric the audit defense stands on: false agreements

Two-reader agreement is only a safety net if agreeing readers are usually *right*. The failure mode
it can't see on its own is a **correlated error** — both readers landing on the same *wrong* value,
so the disagreement check never fires. That's the number worth measuring.

**Zero false agreements across 148 dual-read receipts** (all four corpora), on amount, date, and
merchant. On the domestic corpus specifically: 0 / 31. When the two readers agreed, they were right.
Provisional at this sample size — a single miss would exceed the target gate at n = 148 — so the gate
stays non-binding until the corpus grows, but the first read is clean.

---

## Categorization accuracy

**100% on the buckets the AI is allowed to decide** (accommodation, travel, conference fees, and the
"other" catch-all) at n = 40, via the real vision-plus-text classifier. The three meal buckets
(individual / team / client) are deliberately *not* AI-decided — the split depends on who attended,
which the receipt can't show, so the employee assigns it. The model's authority is scoped to what a
receipt actually reveals; that's a design decision, not a limitation.

---

## PII redaction

A deterministic scrubber removes sensitive fields (card numbers, tax IDs, dates of birth, driver's
licence numbers, home addresses, cardholder names printed beside a card) before any content reaches
a model or a log. A held-out generalization check — running the detectors over receipts they were
never tuned on — surfaced a real leak, which was fixed and locked in with a regression test. (No
sensitive content is shown here or shipped in this repo; the demo runs only on masked images that
were re-scanned to confirm nothing sensitive is machine-readable.)

---

## Automation ceiling (how much actually flows through)

On the real domestic corpus, both readers agree on amount **and** date for ~55% of receipts, and
among those the amount is right **100%** of the time. That ~55% is an *upper bound* on automation:
the full policy pipeline — anomaly scoring, the per-report and rolling-30-day ceilings, unverifiable
dates, and every blocking flag — can only send **more** receipts to a human, never fewer. The system
is deliberately biased toward review; the automation rate is a floor on safety, not a target to max.

(EUR receipts auto-approve at 0%: weak OCR on EUR formatting means the two readers rarely
agree, so nearly everything routes to a human. That's the safety design working, not a
failure — and provisional at n = 12 regardless.)

---

## What these numbers are, and aren't

They are a genuine, reproducible first measurement on a small curated corpus, taken with a real
vision model — enough to show the architecture behaves as designed and that the two-reader premise
holds up. They are **not** a production accuracy claim. The honest path to a real deployment is to
re-run this same evaluation on a far larger and more varied receipt corpus, and to watch the flag
rate (which drives both the automation rate and the review load) on live traffic. The evaluation
harness is what turns each of these claims from *argued* into *demonstrated* — and it's built to keep
doing that as the corpus grows.

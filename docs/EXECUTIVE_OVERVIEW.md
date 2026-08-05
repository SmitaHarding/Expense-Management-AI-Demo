# Executive Overview

## Why this exists

Expense processing is a tax on everyone it touches. Employees retype details a receipt already
shows. Managers approve a queue in which most reports are fine and a few are not, with no quick
way to separate them. Finance carries the audit risk for whatever slips through. The cost is
real but diffuse, which is why it rarely gets fixed properly.

The tempting shortcut — "let AI approve expenses" — trades one problem for a worse one. A model
that reads receipts will occasionally misread a total, mis-key a date, or be nudged toward the
wrong answer by text planted on a receipt. Handing it the authority to release money makes those
mistakes expensive and, more importantly, makes the system impossible to defend in an audit. The
question an auditor asks is not "is the AI usually right?" It is "show me exactly why this specific
payment was approved." A black box cannot answer that.

This project was built around that constraint from the start: **automate the reading and checking,
keep the deciding deterministic and auditable, and put a human at every point of real uncertainty.**

## The value

The system is designed to move most clean reports through with no human touch while sending only
the genuinely uncertain ones to a person — and to make every outcome explainable.

- **Employees stop doing data entry.** Receipts are read automatically; the person confirms rather
  than types.
- **Managers only act on what needs judgment.** Clean reports auto-approve. Managers see the
  auto-approved ones in a weekly digest (informed, not asked to act) and spend their attention on
  the flagged minority, each arriving with the specific issue already highlighted.
- **Finance gets a defensible trail.** Every decision — machine or human — is logged with its
  reason and the exact policy version that produced it, so any report can be replayed and explained.
- **The controls are legible.** Caps, limits, and the approval matrix are written as rules, not
  buried in a model. Anyone can read what the policy is and change it deliberately.

## What was built — and how

The build followed one discipline throughout: **no design decision with a real trade-off was made
by assumption.** Each was raised, argued, and confirmed before code was written, and every policy
change moved the config, the documentation, the tests, and the version number together so they can
never quietly drift apart.

A few decisions worth calling out, because they show the reasoning:

- **Two readers instead of one.** OCR and a vision model read each receipt independently. Measured
  on a real receipt corpus, the vision model is the stronger reader and OCR is the cross-check —
  so the design uses their *agreement* as the gate for automation. When they disagree on the amount
  or the date, that receipt goes to a human. This is what lets the system automate safely: it never
  trusts a single reader with a payment.

- **Merchant was dropped from the automation gate — deliberately.** Requiring the two readers to
  also agree on the *merchant name* was throttling automation without protecting the money (the
  merchant is descriptive, not the amount owed). Removing it lifted the automation rate materially
  while amount and date — the fields that actually matter — still gate every auto-approval.

- **The anomaly check is a scorecard, not machine learning.** A poorly-trained model would be
  opaque *and* wrong, and there isn't enough labeled data to train one well yet. A transparent
  weighted scorecard lets an auditor see exactly why a report scored what it scored. It can only
  ever add a human to a report — it never rejects one on its own.

- **Corrections can't be gamed.** When a person corrects an amount the readers disputed, the true
  value re-runs every policy check — but the original machine disagreement is never erased, so a
  disputed receipt still reaches a human no matter what value is typed.

- **Auto-approval is visible.** Clean reports pay without human review, but a control was added so
  they are never invisible: each is included in a weekly manager visibility digest. This closes the
  "money left with no named human aware of it" gap without re-adding review load.

## What this demo is — and isn't

The public demo runs the **real** policy, routing, and anomaly engine, but on a small curated set
of receipts and on *frozen* extraction reads rather than live OCR and vision — so it is deterministic,
needs no API key, and exposes no sensitive data. It is a faithful illustration of the decision
architecture, not a production deployment.

The path to a real pilot would add live extraction at scale, integration with the payment and HR
systems, and a broader evaluation across a much larger receipt corpus. The architecture — two
readers, deterministic policy, human-in-the-loop, full audit trail — is the part meant to carry
forward unchanged.

# Expense AI — an auditable, human-in-the-loop expense automation demo

A working demo of an AI expense-management system that automates the boring 80% of
expense reports **without** letting a model make the money decision. Two independent
readers extract each receipt, deterministic policy rules decide routing, and a person
stays in the loop wherever the machine isn't certain. Every decision is logged with its
reason and the exact policy version that produced it.

This repository is a **safe, self-contained slice** of a larger design. It ships masked
receipt images and frozen extraction reads, and replays them through the *real* policy and
routing engine — so you can click through the whole flow with **no API key, no file upload,
and no sensitive data**.

> **Try it:** live demo coming soon — or run it locally in two minutes (see [Run it locally](#run-it-locally) below). <!-- TODO(deploy): replace this line with: **[Try the live demo](STREAMLIT_URL)** -->
> **Read the thinking:** [Executive overview](docs/EXECUTIVE_OVERVIEW.md) · [One-page policy](docs/POLICY_ONE_PAGER.md) · [Use-case scenarios](docs/USE_CASE_SCENARIOS.md)

---

## The problem

Expense reports are slow and expensive on both ends. Employees retype what a receipt already
says; finance and managers review a queue where most reports are perfectly fine and a few
are not, with no fast way to tell which is which. The obvious fix — "let AI approve them" —
quietly moves a financial decision to a model that can misread a total or be talked into the
wrong answer. That is the exact thing an auditor will not accept.

## The idea

Automate the *reading and checking*, keep the *deciding* deterministic and auditable, and
put a human at every point of genuine uncertainty.

- **Two readers, not one.** OCR and a vision model read every receipt independently. A report
  can only auto-approve when both readers agree on the fields that matter (amount and date).
  Disagreement is a signal, not an error — it routes the receipt to a person.
- **Policy is code, not a prompt.** Per-diem caps, group-meal limits, an accommodation cap,
  non-reimbursable exclusions, and the approval matrix are deterministic rules in versioned
  config. The model proposes; the rules decide.
- **The score you can read.** The anomaly check is a transparent weighted scorecard, not a
  black-box model — every point of a report's score traces to a named rule an auditor can see.
- **A human where it counts.** At the review checkpoint and the approver view, a person confirms
  categories and corrects any amount or date the two readers disputed. Corrections re-run the
  real policy checks, and a machine disagreement can never be silently "corrected" away.
- **Nothing moves silently.** Clean reports auto-approve, but every auto-approval appears in a
  weekly manager visibility digest — the manager is informed, not asked to act.

## What the demo shows

Six user-visible stages, the same flow an employee and an approver would actually see:

**Capture → Extract + Redact → Validate → Review + Confirm → Policy + Route → Decision**

Three trips are included, each telling a different part of the story:

| Scenario | What it demonstrates | Outcome |
|---|---|---|
| **Clean trip** | Small, in-policy, both readers agree | Auto-approves — with a manager-visibility note |
| **Clean multi-day** | Clean report, just over the auto-approve ceiling | Routes to a manager — routine sign-off, *not* a policy breach |
| **Messy trip** | Readers disagree on amounts and dates; a hotel folio misreads | Routes to review; the approver verifies the true values before approving |

Full detail in [docs/USE_CASE_SCENARIOS.md](docs/USE_CASE_SCENARIOS.md).

## How the "no-key, no-PII" demo works

Live OCR and vision are expensive and non-deterministic, and running real receipts through a
hosted service is a privacy risk. So the demo **freezes** the expensive, non-deterministic
parts — the OCR read, the vision read, the redaction result, the image-quality check — into
`streamlit_demo/demo_replay.json`, captured once offline. At demo time it runs only the
**deterministic downstream engine** (policy caps, approval matrix, anomaly score, routing,
report assembly) live against the trip details you enter.

The receipts you see are **real brand-name receipts with all PII masked** on the image, and
each masked image was re-scanned with OCR + the project's own PII detectors to confirm nothing
sensitive is machine-readable before it shipped. Unmasked originals are never in this repo.

## Run it locally

```bash
git clone <your-fork-url>
cd expense-ai-demo-public
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_demo/app.py
```

That opens the app at `http://localhost:8501`. No API key or receipt upload is required — the
demo runs entirely on the frozen reads.

To sanity-check the engine without the UI:

```bash
python3 streamlit_demo/demo_engine.py --wynn-sweep
```

## What's in this repository

```
streamlit_demo/    the six-stage Streamlit app + the frozen replay data and masked images
src/pipeline/      the real policy / routing / anomaly / validation engine the demo runs on
src/config/        the versioned policy config, flag registry, and report schema
docs/              executive overview, one-page policy, and use-case scenarios
```

The heavy build-time pieces (live OCR, the vision-model client, the evaluation harness) are
deliberately **not** in this public slice — the point here is to show the decision architecture
and let you run it, not to ship the internal tooling.

## A note on scope

This is a demonstration of an approach, not a production expense system. It runs on a small,
curated set of receipts to make the design legible. The [executive overview](docs/EXECUTIVE_OVERVIEW.md)
explains what would change on the path to a real pilot.

## License

See [LICENSE](LICENSE).

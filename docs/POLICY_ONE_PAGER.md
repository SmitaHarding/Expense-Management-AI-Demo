# Expense Policy — One Page

A plain-English summary of the rules the engine enforces. These live as versioned config
(`src/config/policy_config.json`); the values below are the current demo policy. Every rule is
deterministic — the AI proposes categories and reads, but these limits decide the outcome.

## Spending limits

| Rule | Limit | How it's handled |
|---|---|---|
| **Individual meals (per diem)** | $75 / person / day | Auto-capped: pay up to $75/day, the excess is excluded and the employee is told at receipt time. Informational — never blocks. |
| **Team meals** | $40 / attendee | Over the cap routes to review; the overage is not auto-excluded — a person decides. |
| **Client entertainment** | $50 / attendee | Same handling as team meals. |
| **Accommodation** | $300 / night incl. taxes | Per-night = folio total ÷ nights, where nights is read from the folio's check-in/check-out by **both** readers (not self-reported). If the two disagree on the stay dates, or the folio shows none, the check falls back conservatively and routes to a person. Over the cap routes to a manager **and** the travel approver. |
| **Non-reimbursable** | $0 | Alcohol, minibar, in-room entertainment, spa, gym, and personal items are auto-excluded and never paid. |

## When a report auto-approves

A report is paid without human review **only if all four hold**:

1. Report total is under **$200**
2. The employee's rolling 30-day auto-approved total is at or under **$400**
3. **Zero** blocking policy flags
4. Anomaly score is under **0.6**

If any one fails, the report routes to a human. A clean report that simply exceeds these limits
is *routine sign-off*, not a policy breach.

## Who approves what

| Report total | Approver(s) |
|---|---|
| Under $200 (and clean) | Auto-approved |
| $200, up to and including $2,000 | Manager |
| Over $2,000, up to and including $5,000 | Manager + Finance |
| Over $5,000 | Manager + Finance + Travel approver |

Certain policy breaches escalate regardless of amount: an over-cap team meal or client-entertainment
line adds the travel approver, and an over-cap hotel routes to the manager and then the travel approver.

## How receipts are trusted

Two independent readers (OCR and a vision model) read every receipt. A receipt is trusted for
automatic handling only when both readers **agree on the amount and the date**. Any disagreement —
or a receipt one reader couldn't read — routes to a person, who verifies the true value against the
receipt image before approving. A machine disagreement is never erased by a correction.

## The anomaly check

A transparent weighted scorecard (not machine learning) rates how unusual a report looks against the
employee's own history and their peer group. It can only ever **add a human** to a report — it never
approves, rejects, or blocks payment on its own, and its score is never shown as a verdict to the
employee or approver.

## Oversight of automation

Every auto-approved report is included in a **weekly manager visibility digest**. The manager is
informed that these paid without human review — not asked to approve them. Money never leaves without
a named human having visibility, and the review load for clean reports stays at zero.

## Flags

The engine raises **16 blocking flags** (route to a human) and **4 informational flags** (recorded and
visible, consequence already enforced). Blocking examples: reader disagreement, over-cap accommodation,
duplicate receipt, unverifiable date, possible prompt injection. Informational examples: over per-diem
(already auto-capped), a logged human correction.

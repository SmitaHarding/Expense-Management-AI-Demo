# Use-Case Scenarios and Handling

The demo ships three trips, each chosen to show a different part of the decision architecture:
a clean auto-approval, a clean report that needs routine sign-off, and a messy report where the
readers disagree and a human resolves it. Every number below comes from the real engine running
on the frozen reads — nothing is scripted.

Each scenario has two tables: the **receipts** in the trip, and the **stage-by-stage handling**
the system applies.

---

## Scenario 1 — Clean trip → auto-approves

A small, in-policy client visit. Both readers agree on every receipt.

| Receipt | Amount | AI category | Readers agree? |
|---|---|---|---|
| Denny's | $58.44 | Individual meal | Yes (amount + date) |
| Panda Express | $19.07 | *Other* → corrected to Individual meal | Yes (amount + date) |
| **Total** | **$77.51** | | |

| Stage | What happens |
|---|---|
| Capture | Trip context entered; both receipts pass the image-quality gate. |
| Extract + Redact | OCR and the vision model read each receipt; PII is scrubbed (only redaction *types* are kept). |
| Validate | Both readers agree on amount and date for both receipts — nothing routes to a human here. |
| Review + Confirm | The employee corrects Panda from "Other" to "Individual meal." This clears the one thing that would have forced review, and the report flips to auto-approve live. |
| Policy + Route | Total $77.51 is under the $200 ceiling, no blocking flags, anomaly well under threshold. |
| Decision | **Auto-approved.** A note confirms the report is included in the weekly manager visibility digest — no action required. |

**Why it matters:** this is the 80% case. It shows automation working *and* shows the human
checkpoint (the category correction) that keeps the employee in control — plus the oversight
note so the auto-approval is never invisible.

---

## Scenario 2 — Clean multi-day trip → routine manager sign-off

A clean report that simply exceeds the auto-approve ceiling. Nothing is wrong with it.

| Receipt | Amount | Category | Note |
|---|---|---|---|
| Shake Shack | $50.33 | Individual meal | — |
| Wynn (SW Steakhouse) | $402.34 | Team dinner, 11 attendees | $402.34 ÷ 11 = $36.58/head, under the $40 cap |
| **Total** | **$452.67** | | |

| Stage | What happens |
|---|---|
| Validate | Both readers agree on amount and date for both receipts. |
| Review + Confirm | The Wynn receipt is confirmed as a team dinner and the attendee count entered — the split a receipt can't show is employee-assigned. |
| Policy + Route | No caps breached. The only reason it isn't auto-approved is the total: $452.67 is over the $200 ceiling, so it routes to a manager. |
| Decision | Presented to the manager as a **routine sign-off**, shown in a neutral colour — *not* a warning. Zero policy flags. |

**Why it matters:** it draws the line between "needs a human because of size" and "needs a human
because something is wrong." The interface deliberately colours these differently, so approvers
don't learn to treat routine routing as a red flag. The anomaly score is not shown, because here
it adds nothing — the dollar total is the whole story.

---

## Scenario 3 — Messy trip → review, then human-verified approval

Real-world messiness: a hotel folio whose total both readers misread, a car rental the readers
disagree on, and a fuel receipt where one reader couldn't find a date. This is where the two-reader
design earns its keep.

| Receipt | OCR read | Vision read | Disagreement |
|---|---|---|---|
| Cafe Mason | $32.59 | $32.59 | None |
| Parc 55 (Hilton folio) | $198,435.00 | –$1,984.35 | **Amount** — both misread the total badly (the stay dates, check-in Aug 27 / check-out Aug 31, both read correctly) |
| City Rent-A-Car | $17.90 | $184.67 | **Amount** |
| Shell (fuel) | $28.32 | $28.32 | **Date** — OCR read no date at all |

| Stage | What happens |
|---|---|
| Validate | Three receipts flag a reader disagreement (`extraction_mismatch`); the system trusts neither read. |
| Policy + Route | Routes to review. The unverifiable total is never shown as a real number — the report will not commit to a figure it can't trust. |
| Decision — verify amounts | The approver opens each disputed receipt image and keys the true amount (Hilton $1,984.35, car rental $184.67). Each entry re-runs the real policy checks. |
| Decision — verify dates | The approver confirms the one date a reader actually couldn't read (Shell, where OCR found no date). The Hilton folio's stay dates agreed between the readers, so they need no confirmation — they're used directly as the nights for the cap. |
| Decision — a new flag surfaces | With the true Hilton amount entered, the per-night cost is recomputed on the nights read from the folio ($1,984.35 ÷ 4 nights, Aug 27 → Aug 31 = $496/night), which is over the $300/night accommodation cap, so `over_accommodation_cap` fires and the route escalates to manager **and** travel approver. |
| Decision — approve | Only once every disputed amount and date is verified can the approver approve. Any line's receipt can be opened on demand. |

**Why it matters:** the messy case is the safety case. The system doesn't pretend to be certain —
it surfaces exactly what's uncertain, gives the approver the receipt and the tools to resolve it,
and re-checks policy on the *true* numbers. A disagreement is never silently smoothed over.

---

## The handling principles behind all three

| Principle | Where you see it |
|---|---|
| Automate the clean majority | Scenario 1 auto-approves with no human touch. |
| Size ≠ violation | Scenario 2 routes for sign-off but is shown as routine, not a breach. |
| Two readers must agree to auto-trust | Scenario 3's disagreements all route to a person. |
| Human verifies, machine re-checks | The approver keys true values; policy re-runs on them. |
| Disagreement can't be gamed away | The original mismatch persists even after a correction. |
| Nothing pays invisibly | Every auto-approval appears in the manager visibility digest. |
| The approver can always see the evidence | Any line's receipt image is viewable at the decision. |

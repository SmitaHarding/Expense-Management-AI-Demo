#!/usr/bin/env python3
"""Step 5 — Streamlit UI: the six-stage wizard over the demo engine.

Key-free, PII-safe demo of the expense pipeline. Replays the FROZEN reads
(demo_replay.json) through the six user-visible stages and runs the REAL policy/
anomaly/routing engine (demo_engine -> process_report) live against the trip
context and label corrections the user enters. No OCR/vision, no API, no upload.

Run:
    pip install streamlit          # + the pipeline deps (see requirements.txt)
    streamlit run streamlit_demo/app.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "streamlit_demo"))

from pipeline.config_loader import load_pinned_config          # noqa: E402
import demo_engine                                             # noqa: E402

STAGES = ["1 · Capture", "2 · Extract + Redact", "3 · Validate",
          "4 · Review + Confirm", "5 · Policy + Route", "6 · Decision"]
CATEGORIES = ["meal_individual", "meal_group", "client_entertainment",
              "travel", "accommodation", "conference_fees", "other"]
CAT_LABEL = {"meal_individual": "Individual meal", "meal_group": "Team dinner",
             "client_entertainment": "Client entertainment", "travel": "Travel",
             "accommodation": "Accommodation", "conference_fees": "Conference fees",
             "other": "Other / unclear"}


@st.cache_data
def load_replay() -> dict:
    return json.loads((REPO / "streamlit_demo" / "demo_replay.json").read_text())


@st.cache_data
def load_context() -> dict:
    return json.loads((REPO / "streamlit_demo" / "sample_trip_context.json").read_text())


@st.cache_resource
def get_cfg():
    return load_pinned_config()


def set_window(replay: dict, set_id: str) -> tuple[date, date]:
    setdef = next(s for s in replay["sets"] if s["set_id"] == set_id)
    ds = sorted(date.fromisoformat(replay["receipts"][r]["llm"]["date"])
                for r in setdef["receipts"] if replay["receipts"][r]["llm"]["date"])
    return ds[0] - timedelta(days=1), ds[-1] + timedelta(days=1)


def img_path(rel: str) -> str:
    return str(REPO / rel)


# --------------------------------------------------------------------------- #
st.set_page_config(page_title="Expense AI — demo", layout="wide")
replay, base_ctx, cfg = load_replay(), load_context(), get_cfg()
ss = st.session_state
ss.setdefault("set_id", replay["sets"][0]["set_id"])
ss.setdefault("stage", 0)
ss.setdefault("labels", {})           # rid -> {category, attendees}
ss.setdefault("verified_amounts", {}) # rid -> approver-entered amount (Stage 6)
ss.setdefault("verified_dates", {})   # rid -> approver-confirmed date (Stage 6)
ss.setdefault("decision", None)

ANOMALY_THRESHOLD = cfg.policy["auto_approval"]["anomaly_score_threshold"]
AUTO_CEILING = cfg.policy["auto_approval"]["auto_approve_report_max_usd"]


def amount_unverified(rid: str) -> bool:
    """The two readers disagreed on this receipt's amount, so the system won't
    trust either read — the amount is shown as unverified until a human confirms it."""
    return not replay["receipts"][rid]["agreement"].get("amount", False)


def date_unverified(rid: str) -> bool:
    """Date is a gate field. If the two readers disagree — or one couldn't read a
    date at all — a human confirms the date off the receipt before approval.
    Mirrors the amount check so any gate-field disagreement gets a verification
    affordance, not just amount."""
    if "date" not in replay["gate_fields"]:
        return False
    return not replay["receipts"][rid]["agreement"].get("date", False)


def anomaly_is_binding(res: dict) -> bool:
    """True only when the report routes to a human and the anomaly score is the
    ONLY reason — under the auto ceiling, zero blocking flags, but anomaly at/above
    threshold. (Rolling-30d is 0 in the demo, so it never binds here.) This is the
    single case where we surface a neutral pattern-check note to the approver."""
    return (res["route"] != ["auto"]
            and res["report_total_usd"] < AUTO_CEILING
            and not res["blocking_flags"]
            and res["anomaly_score"] >= ANOMALY_THRESHOLD)


def manager_digest_note() -> str | None:
    """APR-07 (config 0.9.5): auto-approved reports are included in a notify-only
    manager visibility digest. Reads the real config setting so the demo only ever
    claims the control the policy actually declares."""
    vis = cfg.policy.get("notifications", {}).get("manager_auto_approve_visibility", {})
    if not vis.get("enabled"):
        return None
    cadence = vis.get("cadence", "weekly")
    return (f"📩 Manager notified — this report is included in the {cadence} manager "
            "visibility digest of auto-approved reports (APR-07). No action required.")


def routing_banner(res: dict, money_txt: str) -> None:
    """One status banner, coloured by WHY the report routes — never by the raw
    `in_review` string (item 2). green=auto · neutral=clean, routed by size/tier ·
    amber=informational flag · red=blocking policy breach. The anomaly number is
    never shown (item 1); only when it's the binding reason do we add a neutral line."""
    route_txt = " → ".join(res["route"])
    if res["route"] == ["auto"]:
        st.success(f"**Auto-approve** — within policy, no human needed.  ({money_txt})")
        note = manager_digest_note()
        if note:
            st.caption(note)
    elif res["blocking_flags"]:
        st.error(f"**Routes to: {route_txt}** — a policy check needs a human.  ({money_txt})")
    elif res["informational_flags"]:
        st.warning(f"**Routes to: {route_txt}** — one item is flagged for a note.  ({money_txt})")
    elif anomaly_is_binding(res):
        st.info(f"**Routes to: {route_txt}** — Sent for review: spending-pattern check needed.  ({money_txt})")
    else:
        st.info(f"**Routes to: {route_txt}** — routine sign-off, no policy issue.  ({money_txt})")

# ---- sidebar: scenario + progress -----------------------------------------
with st.sidebar:
    st.markdown("### Expense AI · demo")
    st.caption(f"config {replay['config_version']} · frozen reads, no API · "
               f"gate {replay['gate_fields']}")
    titles = {s["set_id"]: s["title"] for s in replay["sets"]}
    picked = st.selectbox("Trip scenario", list(titles), format_func=lambda k: titles[k],
                          index=list(titles).index(ss.set_id))
    if picked != ss.set_id:
        ss.set_id = picked
        ss.stage, ss.labels, ss.verified_amounts, ss.verified_dates, ss.decision = 0, {}, {}, {}, None
        w0, w1 = set_window(replay, picked)
        ss.trip_start, ss.trip_end = w0, w1
    st.divider()
    for i, name in enumerate(STAGES):
        st.write(("**➡ " + name + "**") if i == ss.stage else ("✓ " + name if i < ss.stage else "· " + name))
    st.divider()
    if st.button("↻ Restart scenario", width="stretch"):
        ss.stage, ss.labels, ss.verified_amounts, ss.verified_dates, ss.decision = 0, {}, {}, {}, None

setdef = next(s for s in replay["sets"] if s["set_id"] == ss.set_id)
receipt_ids = setdef["receipts"]
w0, w1 = set_window(replay, ss.set_id)
ss.setdefault("trip_start", w0)
ss.setdefault("trip_end", w1)


def build_ctx(include_verified: bool = False) -> dict:
    """Assemble the engine context from the current UI state.

    `include_verified` folds in the approver's Stage-6 amount corrections. Stage 5
    (the AI's pre-human decision) runs without them; Stage 6 (the human resolving)
    runs with them so the corrected total and re-checked policy reflect the truth."""
    labels = {}
    for rid, lab in ss.labels.items():
        entry = {"category_override": lab["category"]}
        if lab.get("attendees"):
            entry["attendees"] = lab["attendees"]
        labels[rid] = entry
    if include_verified:
        for rid, amt in ss.verified_amounts.items():
            if amt:
                labels.setdefault(rid, {})["amount_override"] = amt
    return {
        "trip_id": base_ctx["trip_id"],
        "employee_id": base_ctx["employee_id"],
        "manager_id": base_ctx["manager_id"],
        "trip_start": ss.trip_start.isoformat(),
        "trip_end": ss.trip_end.isoformat(),
        "location": base_ctx["location"],
        "purpose": ss.get("purpose", base_ctx["purpose"]),
        "spend_history": base_ctx["spend_history"],
        "receipt_labels": labels,
    }


def run_engine(include_verified: bool = False):
    ctx = build_ctx(include_verified)
    return demo_engine.run_set(replay, ctx, cfg, ss.set_id)


def nav():
    c1, c2, _ = st.columns([1, 1, 6])
    if ss.stage > 0 and c1.button("← Back"):
        ss.stage -= 1
        st.rerun()
    if ss.stage < len(STAGES) - 1 and c2.button("Next →", type="primary"):
        ss.stage += 1
        st.rerun()


st.markdown(f"## {titles[ss.set_id]}")
st.progress((ss.stage + 1) / len(STAGES), text=STAGES[ss.stage])
stage = ss.stage

# ---- Stage 1 — Capture ----------------------------------------------------
if stage == 0:
    st.markdown("#### Trip details")
    c1, c2, c3 = st.columns(3)
    ss.trip_start = c1.date_input("Trip start", ss.trip_start)
    ss.trip_end = c2.date_input("Trip end", ss.trip_end)
    c3.text_input("Location", base_ctx["location"]["city"], disabled=True)
    ss.purpose = st.text_input("Purpose", ss.get("purpose", base_ctx["purpose"]))
    st.number_input("Typical monthly travel spend ($)", value=base_ctx["spend_history"]["typical_monthly_usd"],
                    help="What to enter: your usual monthly travel spend, the merchants/categories you normally "
                         "use, and how often you travel. Why it matters: it's the baseline the anomaly check "
                         "compares against — an unusually large charge, a new merchant, or a weekend can raise the "
                         "anomaly signal and push an otherwise-clean report to review.")
    st.markdown("#### Receipts submitted")
    st.caption("Quality gate (CAP-09, min 400px) runs at intake. All shown receipts passed.")
    cols = st.columns(len(receipt_ids))
    for col, rid in zip(cols, receipt_ids):
        r = replay["receipts"][rid]
        col.image(img_path(r["display_image"]), width="stretch")
        col.caption(f"{r['llm']['merchant'][:18]} · {'✅ passed' if r['quality']['passed'] else '⛔'}")

# ---- Stage 2 — Extract + Redact -------------------------------------------
elif stage == 1:
    st.caption("Two independent readers (OCR + Claude vision) read each receipt. PII is scrubbed "
               "before anything is stored — only the redaction *types* are shown, never the values.")
    for rid in receipt_ids:
        r = replay["receipts"][rid]
        with st.container(border=True):
            ci, co, cl = st.columns([1, 2, 2])
            ci.image(img_path(r["display_image"]), width="stretch")
            co.markdown("**OCR read**")
            co.write(f"amount: `{r['ocr']['amount']}`  \ndate: `{r['ocr']['date']}`  \n"
                     f"merchant: `{r['ocr']['merchant']}`  \nconfidence: {r['ocr']['confidence']}")
            cl.markdown("**Claude read**")
            cl.write(f"amount: `{r['llm']['amount']}`  \ndate: `{r['llm']['date']}`  \n"
                     f"merchant: `{r['llm']['merchant']}`")
            types = r["redaction"]["types_redacted"]
            st.caption("🔒 redacted: " + (", ".join(types) if types else "nothing sensitive detected"))

# ---- Stage 3 — Validate ---------------------------------------------------
elif stage == 2:
    st.caption("The report auto-approves only if both readers agree on the gate fields "
               f"({', '.join(replay['gate_fields'])}). Disagreement routes the receipt to a human.")
    for rid in receipt_ids:
        r = replay["receipts"][rid]
        a = r["agreement"]
        cols = st.columns([2, 1, 1])
        cols[0].write(f"**{r['llm']['merchant'][:26]}**")
        cols[1].write(("✅ amount agrees" if a["amount"] else "⚠️ amount differs"))
        cols[2].write(("✅ date agrees" if a["date"] else "⚠️ date differs"))
    silent = [rid for rid in receipt_ids
              if all(replay["receipts"][rid]["agreement"].get(f) for f in replay["gate_fields"])]
    st.info(f"{len(silent)} of {len(receipt_ids)} receipts have both readers in agreement.")

# ---- Stage 4 — Review + Confirm -------------------------------------------
elif stage == 3:
    st.caption("Human checkpoint. The AI proposes a category for each line; you confirm or correct it, "
               "and supply attendee counts the receipt can't show (team dinners are employee-assigned).")
    for rid in receipt_ids:
        r = replay["receipts"][rid]
        ai_cat = r.get("ai_category") or "other"
        with st.container(border=True):
            c1, c2, c3 = st.columns([2, 2, 2])
            amt_line = "⚠️ amount unverified" if amount_unverified(rid) else f"${r['llm']['amount']}"
            c1.markdown(f"**{r['llm']['merchant'][:22]}**  \n{amt_line}")
            c1.caption(f"AI proposed: **{CAT_LABEL.get(ai_cat, ai_cat)}**")
            chosen = c2.selectbox("Category", CATEGORIES, index=CATEGORIES.index(ai_cat),
                                  format_func=lambda k: CAT_LABEL[k], key=f"cat_{rid}")
            att = None
            if chosen in ("meal_group", "client_entertainment"):
                att = c3.number_input("Attendees", min_value=1, max_value=50, value=11, key=f"att_{rid}")
            ss.labels[rid] = {"category": chosen, "attendees": att}
            if chosen != ai_cat:
                c1.caption("✏️ corrected")

# ---- Stage 5 — Policy + Route ---------------------------------------------
elif stage == 4:
    res = run_engine()          # AI's pre-human decision — no approver amount corrections yet
    ss.last_result = res
    pending = [rid for rid in receipt_ids if amount_unverified(rid)]
    if pending:
        verified_total = sum(ln["amount"] for ln, rid in zip(res["lines"], receipt_ids)
                             if not amount_unverified(rid))
        money_txt = (f"verified subtotal ${verified_total:.2f} · "
                     f"{len(pending)} line{'s' if len(pending) != 1 else ''} pending human verification")
    else:
        money_txt = f"total ${res['report_total_usd']:.2f}"
    routing_banner(res, money_txt)
    if pending:
        st.caption("The system won't commit to a total it can't verify — the unverified lines "
                   "go to a person on the next stage, who reads the receipt and enters the real amount.")
    if res["blocking_flags"]:
        st.markdown("**Blocking flags (force review):** " + ", ".join(f"`{f}`" for f in res["blocking_flags"]))
    if res["informational_flags"]:
        st.markdown("**Informational:** " + ", ".join(f"`{f}`" for f in res["informational_flags"]))
    st.markdown("#### Lines")
    flags_by_item: dict = {}
    for f in res["flag_detail"]:
        flags_by_item.setdefault(f.get("item_id"), []).append(f["flag"])
    for ln, rid in zip(res["lines"], receipt_ids):
        why = flags_by_item.get(rid, [])
        note = ("  ·  " + ", ".join(f"`{x}`" for x in why)) if why else ""
        amt = "⚠️ unverified — pending human check" if amount_unverified(rid) else f"${ln['amount']}"
        st.write(f"- **{str(ln['merchant'])[:24]}**  {amt}  ·  {CAT_LABEL.get(ln['category'], ln['category'])}  "
                 f"·  {ln['status']}{note}")
    st.caption("Change a category or attendee count on Stage 4 and this decision updates live.")

# ---- Stage 6 — Decision ---------------------------------------------------
elif stage == 5:
    amount_pending = [rid for rid in receipt_ids if amount_unverified(rid)]
    date_pending = [rid for rid in receipt_ids if date_unverified(rid)]

    # Approver verification — amount: read the receipt, enter the true amount for each
    # line the two readers disagreed on. Each entered amount re-runs the real policy engine.
    if amount_pending:
        st.markdown("#### Verify the flagged amounts")
        st.caption("The two readers disagreed on these amounts, so the system didn't trust either "
                   "read. Check each receipt and enter the correct amount to approve.")
        for rid in amount_pending:
            r = replay["receipts"][rid]
            with st.container(border=True):
                ci, cd = st.columns([1, 2])
                ci.image(img_path(r["display_image"]), width="stretch")
                cd.markdown(f"**{r['llm']['merchant'][:30]}**")
                cd.caption(f"Reader A (OCR): `{r['ocr']['amount']}`  ·  Reader B (Claude): "
                           f"`{r['llm']['amount']}`  — these disagree, so neither is trusted.")
                val = cd.number_input("Verified amount from receipt ($)", min_value=0.0, step=0.01,
                                      value=float(ss.verified_amounts.get(rid) or 0.0),
                                      key=f"verify_{rid}")
                ss.verified_amounts[rid] = val
                cd.caption("✅ verified" if val > 0 else "⏳ awaiting your amount")

    # Approver verification — date: date is a gate field, so a disagreement (or a
    # reader that read no date at all) needs a human to confirm the date off the
    # receipt before approval. UI-only: the confirmed date is recorded for the audit
    # trail; it doesn't re-derive policy (dates don't change routing in these sets).
    if date_pending:
        st.markdown("#### Verify the flagged dates")
        st.caption("Date is a gate field. Where the readers disagree — or one couldn't read a date "
                   "at all — confirm the date printed on the receipt before approving.")
        for rid in date_pending:
            r = replay["receipts"][rid]
            ocr_d, llm_d = r["ocr"]["date"], r["llm"]["date"]
            with st.container(border=True):
                ci, cd = st.columns([1, 2])
                ci.image(img_path(r["display_image"]), width="stretch")
                cd.markdown(f"**{r['llm']['merchant'][:30]}**")
                if ocr_d is None or llm_d is None:
                    only = llm_d or ocr_d
                    cd.caption(f"One reader couldn't read a date; the other read `{only}`. "
                               "Confirm the date printed on the receipt.")
                else:
                    cd.caption(f"Reader A (OCR): `{ocr_d}`  ·  Reader B (Claude): `{llm_d}` "
                               "— these disagree. Confirm the date printed on the receipt.")
                default_d = ss.verified_dates.get(rid) or (
                    date.fromisoformat(llm_d or ocr_d) if (llm_d or ocr_d) else ss.trip_start)
                confirmed = cd.date_input("Date printed on the receipt", value=default_d,
                                          key=f"vdate_{rid}")
                ok = cd.checkbox("I've confirmed this date against the receipt", key=f"vdateok_{rid}")
                if ok:
                    ss.verified_dates[rid] = confirmed
                    cd.caption("✅ date confirmed — recorded for the audit trail.")
                else:
                    ss.verified_dates.pop(rid, None)
                    cd.caption("⏳ awaiting your confirmation")

    res = run_engine(include_verified=True)     # corrected amounts re-check policy + total
    ss.last_result = res
    still_amount = [rid for rid in amount_pending if not ss.verified_amounts.get(rid)]
    still_date = [rid for rid in date_pending if rid not in ss.verified_dates]
    still_pending = still_amount + still_date

    st.divider()
    if still_pending:
        bits = []
        if still_amount:
            bits.append(f"{len(still_amount)} amount{'s' if len(still_amount) != 1 else ''}")
        if still_date:
            bits.append(f"{len(still_date)} date{'s' if len(still_date) != 1 else ''}")
        st.info(f"Still to verify before you can approve: {' and '.join(bits)}.")
    elif res["route"] == ["auto"]:
        st.success("### ✅ Approved (auto)\nClean report, within policy — paid in the next weekly run.")
        note = manager_digest_note()
        if note:
            st.caption(note)
    else:
        routing_banner(res, f"total ${res['report_total_usd']:.2f}")
        st.markdown(f"**Amount to approve: ${res['report_total_usd']:.2f}**")
        st.caption("The approver sees a fully extracted, redacted, categorized, policy-checked report — "
                   "one decision, not 15 minutes of data entry. Open any line to see its receipt.")
        for ln, rid in zip(res["lines"], receipt_ids):
            st.write(f"- {str(ln['merchant'])[:24]}  ${ln['amount']}  ·  {CAT_LABEL.get(ln['category'], ln['category'])}")
            # CAT-08 v2: for a hotel folio, show where the per-night cap divisor came
            # from — the check-in/check-out both readers agreed on, and the derived nights.
            if ln.get("category") == "accommodation" and ln.get("nights"):
                st.caption(f"    Hotel stay: {ln['check_in']} → {ln['check_out']} "
                           f"({ln['nights']} night{'s' if ln['nights'] != 1 else ''}, read from the folio) "
                           f"— the per-night cap divides by these nights.")
            with st.expander("View receipt"):
                st.image(img_path(replay["receipts"][rid]["display_image"]), width="stretch")
        if res["blocking_flags"]:
            st.markdown("**Flags to check:** " + ", ".join(f"`{f}`" for f in res["blocking_flags"]))
        if amount_pending:
            st.caption("Verifying the amounts re-ran the policy checks on the true numbers — that's why "
                       "an over-cap line can surface a new flag here.")
        if ss.decision is None:
            c1, c2, _ = st.columns([1, 1, 5])
            if c1.button("✅ Approve", type="primary"):
                ss.decision = "approved"
                st.rerun()
            if c2.button("⛔ Reject"):
                ss.decision = "rejected"
                st.rerun()
        elif ss.decision == "approved":
            st.success("### ✅ Approved by reviewer")
        else:
            st.error("### ⛔ Rejected — returned to employee")

st.divider()
nav()

# Persistent footer: one unobtrusive way out for a demo-only viewer to reach the repo,
# the design docs, and the evaluation results — without putting any eval numbers in the
# interactive flow itself (that stays clean by design).
st.divider()
st.caption(
    "This demo replays frozen OCR/vision reads through the *real* policy engine — "
    "no API key, no uploads, no sensitive data. Full design & "
    "[evaluation results](https://github.com/SmitaHarding/expense-management-ai-demo) on GitHub."
)

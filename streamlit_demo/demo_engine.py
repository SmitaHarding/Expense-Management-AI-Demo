#!/usr/bin/env python3
"""Step 4 — demo adapter.

Runs the REAL pipeline (`process_report`) live against the FROZEN reads
(`demo_replay.json`) plus the trip context and per-receipt labels the user enters.
No live OCR/vision, no API, no new decision logic — the adapter only builds the
fixture the existing pipeline already expects and swaps in a reader that returns
the frozen OCR values so dual-path agreement reproduces exactly what the freeze
recorded.

Two readers:
  * MockExtractor (pipeline default) reads the fixture `truth` block = the frozen
    LLM read (amount = receipt_total, date, merchant, the confirmed category).
  * FrozenOCR (here) returns the frozen OCR read per receipt, so the
    `extraction_mismatch` gate fires exactly where the two frozen paths disagreed.

Label overrides (Stage 4, "Review + Confirm"): the user can correct the AI's
proposed category and supply attendee counts the receipt can't show — the
designed human-in-the-loop step (VER-01; meal subtypes are employee-assigned per
requirements v1.11). E.g. wynn: AI read "accommodation"; the user marks it a team
dinner with 11 attendees -> within the $40/head cap -> counts in full -> the trip
routes to a manager on total.

CLI:
    python3 streamlit_demo/demo_engine.py            # run all sets, write last_report_output.json
    python3 streamlit_demo/demo_engine.py --set clean_multiday
    python3 streamlit_demo/demo_engine.py --wynn-sweep 6 9 11   # show attendees flipping the route
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent          # expense-ai-demo/
sys.path.insert(0, str(REPO / "src"))

from pipeline.config_loader import load_pinned_config     # noqa: E402
from pipeline.extraction import PROMPT_VERSION            # noqa: E402
from pipeline.orchestrator import process_report          # noqa: E402

REPLAY_PATH = REPO / "streamlit_demo" / "demo_replay.json"
CONTEXT_PATH = REPO / "streamlit_demo" / "sample_trip_context.json"
OUT_PATH = REPO / "streamlit_demo" / "last_report_output.json"


class FrozenOCR:
    """Second reader for `process_report`: returns the FROZEN OCR read per receipt
    (by receipt_id) so dual-path agreement reproduces the freeze. The extractor
    (LLM) side stays the pipeline's MockExtractor over the `truth` block."""

    model_version = "frozen-ocr-1.0"

    def __init__(self, ocr_by_id: dict):
        self._ocr = ocr_by_id

    def extract(self, receipt: dict) -> dict:
        o = self._ocr[receipt["receipt_id"]]
        item_id = receipt["truth"]["items"][0]["item_id"]
        item = {"item_id": item_id, "date": o.get("date"), "merchant": o.get("merchant"),
                "amount": o.get("amount"), "currency": receipt.get("currency", "USD"),
                "category": receipt["truth"]["items"][0]["category"], "proof": {"type": "receipt"}}
        # CAT-08 v2: carry the frozen OCR folio stay range so the orchestrator's
        # check_folio_stay_range can compare it against the LLM read (both paths).
        for f in ("check_in", "check_out"):
            if o.get(f) is not None:
                item[f] = o[f]
        return {"items": [item], "model_version": self.model_version,
                "prompt_version": PROMPT_VERSION, "injection_suspects": [],
                "raw_confidence": o.get("confidence", 0.9)}


def _auto_trip_window(item_dates: list[str], grace_days: int = 1) -> tuple[str, str]:
    """Bracket the trip window around the set's receipt dates (min..max +/- grace)
    so travel-window checks don't spuriously fire on a demo trip that spans the
    curated receipts. In the Streamlit UI (Step 5) the user enters these directly."""
    ds = sorted(d for d in item_dates if d)
    if not ds:
        return "2023-01-01", "2023-12-31"
    lo = date.fromisoformat(ds[0]) - timedelta(days=grace_days)
    hi = date.fromisoformat(ds[-1]) + timedelta(days=grace_days)
    return lo.isoformat(), hi.isoformat()


def build_fixture(replay: dict, ctx: dict, set_id: str,
                  label_overrides: dict | None = None) -> tuple[dict, dict]:
    """Assemble the fixture `process_report` expects from frozen reads + context.
    `label_overrides` lets a caller (or the CLI sweep) override per-receipt labels
    on top of the context's `receipt_labels` — used for the wynn attendee sweep."""
    setdef = next(s for s in replay["sets"] if s["set_id"] == set_id)
    labels = dict(ctx.get("receipt_labels", {}))
    for rid, lab in (label_overrides or {}).items():
        labels[rid] = {**labels.get(rid, {}), **lab}

    receipts, ocr_by_id, item_dates, corrections = [], {}, [], {}
    for rid in setdef["receipts"]:
        r = replay["receipts"][rid]
        lab = labels.get(rid, {})
        category = lab.get("category_override") or r.get("ai_category") or "other"
        # The frozen reads are used AS-IS (never faked). A Stage-6 approver amount
        # correction is passed to the REAL pipeline as an items[].correction (v0.9.4),
        # exactly like the production path: the human value is authoritative and
        # re-runs policy, but the raw OCR/LLM reads are left untouched so a genuine
        # extraction_mismatch still fires — the demo shows the true behaviour, not a
        # stand-in that pretends the two readers agreed.
        amount = r["llm"]["amount"]
        if lab.get("amount_override") is not None:
            corrections[rid] = {
                "source": "approver", "field": "amount",
                "human_value": float(lab["amount_override"]),
                "actor_id": "demo_approver",
                "at": "2026-07-30T00:00:00Z",
            }
        item = {
            "item_id": rid,
            "date": r["llm"]["date"],
            "merchant": r["llm"]["merchant"],
            "amount": amount,
            "currency": r.get("currency", "USD"),
            "category": category,
            "proof": {"type": "receipt"},
        }
        if lab.get("attendees"):
            item["attendees"] = [f"person_{i + 1}" for i in range(int(lab["attendees"]))]
        # CAT-08 v2: the folio stay range (check_in/check_out) comes from the frozen
        # LLM read; the orchestrator trusts it onto the item only when the OCR read
        # agrees, then derives nights = check_out - check_in for the per-night cap.
        # (The deprecated employee-entered `nights` label is no longer used.)
        for f in ("check_in", "check_out"):
            if r["llm"].get(f) is not None:
                item[f] = r["llm"][f]
        item_dates.append(r["llm"]["date"])
        q = r["quality"]
        receipts.append({
            "receipt_id": rid,
            "sha256": f"demo-{rid}",   # unique per receipt so the dedup pre-gate doesn't collapse the set
            "trip_id": ctx["trip_id"],
            "language": "en",
            "currency": r.get("currency", "USD"),
            "image_meta": {"short_side_px": q["short_side_px"], "blur_laplacian": q["blur_score"],
                           "glare_pct": 0, "full_frame": True},
            # The extractor (LLM/Claude) is the confident path; OCR confidence is a
            # separate signal carried on the frozen OCR read, not the extraction gate.
            "mock_confidence": 0.95,
            "truth": {"items": [item], "receipt_total": amount},
        })
        ocr_by_id[rid] = r["ocr"]

    sh = ctx.get("spend_history", {})
    ts, te = ctx.get("trip_start"), ctx.get("trip_end")
    if not (ts and te):
        ts, te = _auto_trip_window(item_dates)
    fixture = {
        "report_id": f"DEMO-{set_id}",
        "trip_id": ctx["trip_id"],
        "employee_id": ctx.get("employee_id", "emp_demo"),
        "manager_of_record_id": ctx.get("manager_id", "mgr_demo"),
        "trip": {"location": ctx.get("location", {}).get("city", ""), "start_date": ts,
                 "end_date": te, "reason": ctx.get("purpose", "")},
        "employee_history_totals": sh.get("employee_history_totals", []),
        "peer_group_avg": sh.get("peer_group_avg"),
        "employee_known_merchants": sh.get("known_merchants", []),
        "rolling_30d_auto_approved_total": sh.get("rolling_30d_auto_approved_usd", 0.0),
        "receipts": receipts,
    }
    if corrections:
        fixture["corrections"] = corrections
    return fixture, ocr_by_id


def _nights_between(check_in, check_out) -> int | None:
    """Derived nights for display (mirrors policy_engine._accommodation_nights):
    check_out - check_in, floored at 1. None when the folio range isn't present."""
    if not (check_in and check_out):
        return None
    try:
        return max(1, (date.fromisoformat(check_out) - date.fromisoformat(check_in)).days)
    except (ValueError, TypeError):
        return None


def run_set(replay: dict, ctx: dict, cfg, set_id: str, label_overrides: dict | None = None) -> dict:
    fixture, ocr_by_id = build_fixture(replay, ctx, set_id, label_overrides)
    result = process_report(fixture, cfg, ocr=FrozenOCR(ocr_by_id))
    rep = result["expense_report"]
    proc = rep["processing"]
    flags = rep.get("flags", [])
    blocking_set = set(cfg.blocking_flags())
    return {
        "set_id": set_id,
        "route": proc["route"],
        "report_status": proc["report_status"],
        "report_total_usd": proc["totals"]["included_usd"],
        "anomaly_score": proc.get("anomaly_score"),
        "blocking_flags": sorted({f["flag"] for f in flags if f["flag"] in blocking_set}),
        "informational_flags": sorted({f["flag"] for f in flags if f["flag"] not in blocking_set}),
        "lines": [{"merchant": it.get("merchant"), "amount": it.get("amount"),
                   "category": it.get("category"), "status": it.get("status"),
                   # CAT-08 v2: the reader-agreed folio stay range (present only on
                   # accommodation lines whose two readers agreed) + derived nights,
                   # so the UI can show where the per-night divisor came from.
                   "check_in": it.get("check_in"), "check_out": it.get("check_out"),
                   "nights": _nights_between(it.get("check_in"), it.get("check_out"))}
                  for it in rep["items"]],
        "flag_detail": [{"flag": f["flag"], "item_id": f.get("item_id"), "detail": f.get("detail")}
                        for f in flags],
    }


def _print_summary(s: dict) -> None:
    print(f"\n=== {s['set_id']}  ->  route={s['route']}  status={s['report_status']}  "
          f"total=${s['report_total_usd']:.2f}  anomaly={s['anomaly_score']}")
    if s["blocking_flags"]:
        print(f"    blocking:      {s['blocking_flags']}")
    if s["informational_flags"]:
        print(f"    informational: {s['informational_flags']}")
    for ln in s["lines"]:
        print(f"    - {str(ln['merchant'])[:22]:22s} ${ln['amount']:>10}  {ln['category']:18s} {ln['status']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", help="run only this set_id")
    ap.add_argument("--wynn-sweep", nargs="*", type=int,
                    help="run clean_multiday at these attendee counts to show the route flip")
    args = ap.parse_args()

    replay = json.loads(REPLAY_PATH.read_text())
    ctx = json.loads(CONTEXT_PATH.read_text())
    cfg = load_pinned_config()
    print(f"config v{cfg.config_version} · deterministic (frozen reads, no API) · gate {replay['gate_fields']}")

    if args.wynn_sweep:
        print("\n# wynn attendee sweep (team dinner $402.34; $40/head cap):")
        for n in args.wynn_sweep:
            s = run_set(replay, ctx, cfg, "clean_multiday", {"wynn_20231209_014": {"attendees": n}})
            print(f"  {n:2d} attendees -> ${402.34 / n:5.2f}/head -> route={s['route']} "
                  f"({'clean' if not s['blocking_flags'] else 'blocked: ' + ','.join(s['blocking_flags'])})")
        return 0

    set_ids = [args.set] if args.set else [s["set_id"] for s in replay["sets"]]
    out = {}
    for sid in set_ids:
        s = run_set(replay, ctx, cfg, sid)
        _print_summary(s)
        out[sid] = s
    OUT_PATH.write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nWROTE {OUT_PATH.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

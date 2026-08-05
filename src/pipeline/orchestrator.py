"""Ties every stage together in the fixed order ai_architecture.md §3
specifies — there is no dynamic control flow. Given the same input fixture,
config version, and model versions, this produces byte-identical output
(EVAL-01 replayability).

Two return shapes, deliberately kept separate:
  - `expense_report`  — schema-valid, USD-only items that made it through
    every gate. This is the artifact the approval/payment stages consume.
  - `pre_gate_outcomes` — receipts that never reached extraction (failed
    image quality, non-USD/non-English, or a same-trip duplicate). These
    belong in a manual intake queue / retake flow, not the report schema —
    exactly why the schema's `currency` field is a hard `const: "USD"`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .anomaly import compute_anomaly_score
from .categorize import MockCategorizer, apply_categorization
from .config_loader import PinnedConfig
from .extraction import MockExtractor, MockOCR
from .policy_engine import apply_policy
from .pre_gates import run_pre_gates
from .redact import redact_text, redaction_summary
from .router import determine_route
from .validation import (
    check_arithmetic,
    check_dual_path_agreement,
    check_folio_stay_range,
    normalize_amount,
    scan_injection_suspects,
)


def _scrub_receipt(receipt: dict) -> tuple[dict, dict]:
    """Stage 0 — Redact (CAP-SEC-05), confirmed hard sequencing requirement:
    must run, and be provably run, before the receipt is handed to any
    extractor. Operates on `receipt['raw_ocr_text']` if present (the
    demo's stand-in for whatever raw text a real OCR/scan pass would
    produce) — the receipt dict handed to the extractor afterward carries
    only the redacted version, never the original. See tests/test_redact.py
    for the ordering proof (asserts on what the mock extractor actually
    received, not just on final output) and redact.py's module docstring
    for the confirmed v1 scope limit on real photo/pixel content (Track B).
    """
    raw_text = receipt.get("raw_ocr_text")
    if raw_text is None:
        return receipt, {"applied": False, "redaction_counts": {}}
    result = redact_text(raw_text)
    scrubbed = dict(receipt)
    scrubbed["raw_ocr_text"] = result.redacted_text
    return scrubbed, redaction_summary(result)


def process_report(fixture: dict, cfg: PinnedConfig, extractor=None, ocr=None, categorizer=None,
                    seen_hashes: dict | None = None, trip_reports: dict | None = None) -> dict:
    """`seen_hashes` is the cross-report duplicate registry (CAP-SEC-03).
    `trip_reports` is the cross-report per-diem + supplemental-submission
    registry (T10b, v0.8.0): keyed by (employee_id, trip_id), values are
    lists of {"report_id": str, "per_diem_by_day": {date: amount}} entries
    — one per *approved* report seen so far for that employee/trip. It
    drives two things: (1) per-diem aggregation — the day-level totals
    across all entries become this report's prior_approved_usd /
    prior_approved_from_reports; (2) the supplemental_report_after_trip_approval
    blocking flag — fires whenever the list for this (employee_id, trip_id)
    is non-empty, i.e. this is not the first report seen for the trip.
    Only this function's own approved outcome ever appends an entry — a
    report that lands in human review never contributes, so a sibling still
    under review can neither suppress another report's allowance nor trigger
    the supplemental flag on one, since it might still be rejected or
    withdrawn. Pass the same dict across multiple process_report calls (in
    the order reports were actually approved) to simulate aggregation across
    a trip; omit it to process a single report in isolation — it defaults to
    a fresh empty dict per call, which reproduces pre-T10b behavior exactly
    for every existing fixture."""
    extractor = extractor or MockExtractor()
    ocr = ocr or MockOCR()
    categorizer = categorizer or MockCategorizer()
    seen_hashes = {} if seen_hashes is None else seen_hashes
    trip_reports = {} if trip_reports is None else trip_reports
    trip_id = fixture.get("trip_id", fixture.get("report_id", str(uuid.uuid4())))
    employee_id = fixture["employee_id"]
    report_id = fixture.get("report_id", str(uuid.uuid4()))
    registry_key = (employee_id, trip_id)
    sibling_reports = trip_reports.get(registry_key, [])
    has_approved_sibling = len(sibling_reports) > 0

    prior_approved_totals: dict = {}
    prior_approved_from_reports: dict = {}
    for sibling in sibling_reports:
        for day, amount in sibling["per_diem_by_day"].items():
            prior_approved_totals[day] = prior_approved_totals.get(day, 0.0) + amount
            prior_approved_from_reports.setdefault(day, []).append(
                {"report_id": sibling["report_id"], "amount_usd": amount}
            )

    # VER-03 / REV-04 (v0.9.4): human in-place corrections, keyed by item_id.
    # Each is {source, field, human_value, actor_id, at}. human_value is
    # authoritative and re-runs every downstream check (it is written onto the
    # item's amount below, so apply_policy/anomaly/routing all see the corrected
    # number). The raw OCR/LLM reads are NEVER touched, so a pre-existing
    # extraction_mismatch persists regardless of what a human types — a receipt
    # whose two readers disagreed still routes to a human (anti-gaming).
    corrections = fixture.get("corrections", {})

    pre_gate_outcomes = []
    items: list[dict] = []
    stage_flags: list[dict] = []
    injection_suspects: list[str] = []
    pii_redaction_totals: dict = {}
    pii_redaction_applied = False

    for receipt in fixture["receipts"]:
        gate = run_pre_gates(receipt, cfg, seen_hashes)
        pre_gate_outcomes.append({
            "receipt_id": gate.receipt_id, "passed": gate.passed,
            "routed_to": gate.routed_to, "reasons": gate.reasons,
        })
        if not gate.passed:
            continue  # retake_request / manual_intake_queue / declined_silent — never reaches extraction

        # Redact BEFORE extraction — hard sequencing requirement, enforced
        # here as code (the extractor only ever receives `receipt`, which by
        # this point has already been through _scrub_receipt), not just
        # documented as an intent.
        receipt, receipt_redaction = _scrub_receipt(receipt)
        if receipt_redaction["applied"]:
            pii_redaction_applied = True
            for k, v in receipt_redaction["redaction_counts"].items():
                pii_redaction_totals[k] = pii_redaction_totals.get(k, 0) + v

        llm_out = extractor.extract(receipt)
        ocr_out = ocr.extract(receipt)
        injection_suspects.extend(llm_out.get("injection_suspects", []))

        llm_items_by_id = {i["item_id"]: i for i in llm_out["items"]}
        ocr_items_by_id = {i["item_id"]: i for i in ocr_out["items"]}

        # arithmetic check runs against the LLM path's items vs. the receipt's stated total
        truth = receipt["truth"]
        arithmetic_ok, arithmetic_detail = check_arithmetic(
            truth.get("receipt_total", sum(i["amount"] for i in llm_out["items"])),
            llm_out["items"], cfg,
            tax=truth.get("tax", 0.0), tip=truth.get("tip", 0.0),
        )
        if not arithmetic_ok:
            stage_flags.append({"flag": "arithmetic_check_failed", "item_id": None,
                                 "detail": f"receipt {receipt['receipt_id']}: {arithmetic_detail}"})

        if gate.reasons and any("cross_report_duplicate_flagged" in r for r in gate.reasons):
            stage_flags.append({"flag": "duplicate_receipt", "item_id": None,
                                 "detail": gate.reasons[0]})

        for item_id, llm_item in llm_items_by_id.items():
            ocr_item = ocr_items_by_id.get(item_id, llm_item)
            agree, mismatches = check_dual_path_agreement(llm_item, ocr_item, cfg)
            if not agree:
                stage_flags.append({"flag": "extraction_mismatch", "item_id": item_id,
                                     "detail": "; ".join(mismatches)})

            # CAT-08 v2 (v0.9.6): folio stay-range agreement. A hotel folio's
            # nights divisor is derived from check_in/check_out read by BOTH
            # paths. Trust the range only when the readers agree; a genuine
            # disagreement reuses extraction_mismatch (D3), and anything not
            # trusted is left off the item so the cap uses the conservative
            # nights=1 divisor (D1a) rather than a disputed range.
            stay_trusted, stay_conflict, stay_detail = check_folio_stay_range(llm_item, ocr_item)
            if stay_conflict:
                stage_flags.append({"flag": "extraction_mismatch", "item_id": item_id,
                                     "detail": stay_detail})
            if llm_out["raw_confidence"] < cfg.policy["extraction"]["extraction_confidence_min"]:
                stage_flags.append({"flag": "low_confidence_extraction", "item_id": item_id,
                                     "detail": f"extraction confidence {llm_out['raw_confidence']} below "
                                               f"{cfg.policy['extraction']['extraction_confidence_min']}"})

            item = dict(llm_item)
            item.setdefault("proof", {"type": "receipt"})
            item.setdefault("currency", "USD")
            # EXT-05a (T4): preserve the pre-normalization amount string for
            # audit. The LLM path returns a numeric amount already, so record
            # its string form; if an amount ever arrives as a string, normalize
            # it currency-aware here so downstream math never sees a raw string.
            amt = item.get("amount")
            if isinstance(amt, str):
                item["amount"], item["amount_raw"] = normalize_amount(amt, item.get("currency"))
            else:
                item.setdefault("amount_raw", str(amt))

            # VER-03 / REV-04 (v0.9.4): apply a human amount correction, if one
            # was keyed for this item. The human value supersedes the extracted
            # amount for all downstream policy/total/anomaly/routing checks and
            # is logged as (ai_value -> human_value) for the audit trail + label.
            corr = corrections.get(item_id)
            if corr and corr.get("field") == "amount":
                ai_value = item.get("amount")
                human_value = round(float(corr["human_value"]), 2)
                # VER-03 employee guard: an employee amount that equals NEITHER
                # extraction path exactly (to the cent) is a fraud vector -> the
                # already-registered blocking correction_mismatch routes it to a
                # human. Reviewer/fin_ops corrections are authoritative (no guard).
                if corr.get("source") == "employee":
                    reads = {round(float(x), 2) for x in (llm_item.get("amount"), ocr_item.get("amount"))
                             if isinstance(x, (int, float))}
                    if human_value not in reads:
                        stage_flags.append({"flag": "correction_mismatch", "item_id": item_id,
                                             "detail": f"employee amount correction {human_value} matches neither "
                                                       f"extraction path (OCR {ocr_item.get('amount')}, "
                                                       f"LLM {llm_item.get('amount')}) — VER-03"})
                item["amount"] = human_value
                item["amount_raw"] = str(human_value)
                item["correction"] = {
                    "source": corr.get("source"), "field": "amount",
                    "ai_value": ai_value, "human_value": human_value,
                    "actor_id": corr.get("actor_id") or "unknown",
                    "at": corr.get("at") or datetime.now(timezone.utc).isoformat(),
                }
                stage_flags.append({"flag": "field_corrected", "item_id": item_id,
                                     "detail": f"amount corrected by {corr.get('source')} "
                                               f"({item['correction']['actor_id']}): "
                                               f"AI {ai_value} -> {human_value}"})

            # CAT-08 v2: keep the folio stay range on the item only when both
            # readers agreed on it (stay_trusted). Otherwise strip it so the
            # accommodation cap derives a conservative nights=1 divisor rather
            # than dividing by a disputed or half-read range.
            if stay_trusted:
                item["check_in"] = llm_item.get("check_in")
                item["check_out"] = llm_item.get("check_out")
            else:
                item.pop("check_in", None)
                item.pop("check_out", None)

            item["confidence"] = {"extraction": llm_out["raw_confidence"], "categorization": 0.0}
            items.append(item)

    if injection_suspects:
        stage_flags.append({"flag": "possible_injection", "item_id": None,
                             "detail": f"quarantined text: {injection_suspects}"})

    context = {"simulated_category_error": fixture.get("simulated_category_error")}
    categorized_items = [apply_categorization(i, categorizer, cfg, context) for i in items]

    # CAT-01a: any item that lands in the catch-all bucket gets the registered
    # blocking flag `other_bucket_review`, so it always routes to human review
    # and can never auto-approve — the guarantee the flag registry and
    # categorize.py's docstring both make. This flag was registered as blocking
    # but never actually emitted, so a benign "other" item (small amount, known
    # merchant, low anomaly) could silently auto-approve. Emitting it here closes
    # that code-vs-policy gap (T14, 2026-07-23).
    catch_all = cfg.policy["catch_all_category"]
    for it in categorized_items:
        if it["category"] == catch_all:
            stage_flags.append({
                "flag": "other_bucket_review",
                "item_id": it.get("item_id"),
                "detail": f"item in catch-all '{catch_all}' bucket "
                          f"(categorization confidence {it['confidence']['categorization']}) — "
                          f"always human-reviewed, never auto-approved (CAT-01a)",
            })

    policy_result = apply_policy(
        categorized_items, fixture["trip"], cfg,
        prior_approved_totals=prior_approved_totals,
        prior_approved_from_reports=prior_approved_from_reports,
        has_approved_sibling=has_approved_sibling,
    )
    all_flags = stage_flags + policy_result["flags"]

    report_total = policy_result["totals"]["included_usd"]
    anomaly_score, anomaly_factors = compute_anomaly_score(
        report_total=report_total,
        items=policy_result["items"],
        cfg=cfg,
        employee_history_totals=fixture.get("employee_history_totals"),
        peer_group_avg=fixture.get("peer_group_avg"),
        employee_known_merchants=set(fixture.get("employee_known_merchants", [])),
    )

    routing = determine_route(
        report_total=report_total,
        flags=all_flags,
        rolling_30d_total=fixture.get("rolling_30d_auto_approved_total", 0.0) + report_total,
        anomaly_score=anomaly_score,
        cfg=cfg,
    )

    report_status = "approved" if routing["route"] == ["auto"] else "in_review"

    # T10b: only an approved report's reimbursed amounts feed forward into
    # the shared registry — as a per-diem contribution AND as the trigger
    # for a future sibling's supplemental-report flag. A report still in
    # human review must never suppress another report's allowance or mark
    # a sibling as supplemental (confirmed decision, 2026-07-16), since it
    # could still be rejected or withdrawn.
    if report_status == "approved":
        per_diem_by_day = {
            day: position["reimbursed_usd"]
            for day, position in policy_result["per_diem_position"].items()
            if position["reimbursed_usd"] > 0
        }
        trip_reports.setdefault(registry_key, []).append({
            "report_id": report_id,
            "per_diem_by_day": per_diem_by_day,
        })

    expense_report = {
        "report_id": report_id,
        "trip_id": trip_id,
        "schema_version": cfg.schema.get("title", "") and "0.9.0",
        "submission": {
            "received_at": fixture.get("received_at", datetime.now(timezone.utc).isoformat()),
            "channel": "slack",
            "sender_email": fixture.get("sender_email", f"{fixture['employee_id']}@example.com"),
            "sender_verified": True,
            "employee_id": fixture["employee_id"],
            "manager_of_record_id": fixture["manager_of_record_id"],
        },
        "trip": fixture["trip"],
        "items": policy_result["items"],
        "flags": all_flags,
        "injection_suspects": injection_suspects,
        "processing": {
            "config_version": cfg.config_version,
            "model_version": extractor.model_version if hasattr(extractor, "model_version") else "unknown",
            "prompt_version": "demo-v1",
            "report_status": report_status,
            "anomaly_factors": anomaly_factors,
            "per_diem_position": policy_result["per_diem_position"],
            "totals": policy_result["totals"],
            "anomaly_score": anomaly_score,
            "route": routing["route"],
            "pii_redaction": {"applied": pii_redaction_applied, "redaction_counts": pii_redaction_totals},
        },
    }

    return {
        "expense_report": expense_report,
        "pre_gate_outcomes": pre_gate_outcomes,
        "routing_detail": routing,
    }

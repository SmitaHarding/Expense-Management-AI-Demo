"""Stage 0 — Pre-gates. Zero AI. Mirrors ai_architecture.md §3 stage [0].

Three deterministic gates run before anything enters the AI sandbox:
  1. Image quality (CAP-09)      — resolution, blur, glare, framing
  2. Language / currency (EXT-10) — non-English or non-USD -> manual queue
  3. Duplicate check (CAP-SEC-03/04a) — non-blocking, same-trip vs cross-report

This module never calls a model. Every receipt that reaches Stage 2
(extraction) has already passed all three.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config_loader import PinnedConfig


@dataclass
class PreGateResult:
    receipt_id: str
    passed: bool
    routed_to: str  # "extraction" | "retake_request" | "manual_intake_queue" | "declined_silent" | "flagged_duplicate"
    reasons: list = field(default_factory=list)


def check_image_quality(receipt: dict, cfg: PinnedConfig) -> tuple[bool, list[str]]:
    """CAP-09. Fails fast, while the receipt is still in the employee's hand."""
    q = cfg.policy["capture"]["image_quality"]
    meta = receipt.get("image_meta", {})
    reasons = []

    if meta.get("short_side_px", 0) < q["min_short_side_px"]:
        reasons.append(f"resolution {meta.get('short_side_px')}px < min {q['min_short_side_px']}px")
    if meta.get("blur_laplacian", 999) < q["blur_laplacian_min"]:
        reasons.append(f"blur score {meta.get('blur_laplacian')} < min {q['blur_laplacian_min']}")
    if meta.get("glare_pct", 0) > q["glare_max_area_pct"]:
        reasons.append(f"glare {meta.get('glare_pct')}% > max {q['glare_max_area_pct']}%")
    if q["require_full_frame"] and not meta.get("full_frame", True):
        reasons.append("receipt not fully framed")

    return (len(reasons) == 0, reasons)


def check_language_currency(receipt: dict, cfg: PinnedConfig) -> tuple[bool, list[str]]:
    """EXT-10. This is where your EUR receipts are meant to fail — on purpose.

    v1 policy is USD/English only (base_currency in policy_config.json).
    A non-USD receipt is not a bug in this demo; it is the language gate
    working as designed and routing to the manual intake queue.
    """
    gate = cfg.policy["language_gate"]
    reasons = []
    language = receipt.get("language", "en")
    currency = receipt.get("currency", "USD")

    if language not in gate["allowed_languages"]:
        reasons.append(f"language '{language}' not in allowed {gate['allowed_languages']}")
    if currency not in gate["allowed_currencies"]:
        reasons.append(f"currency '{currency}' not in allowed {gate['allowed_currencies']}")

    return (len(reasons) == 0, reasons)


def check_duplicate(receipt: dict, seen_hashes: dict, cfg: PinnedConfig) -> tuple[str, Optional[str]]:
    """CAP-SEC-03/04a. Non-blocking: dedup completes async in the real system;
    here it runs synchronously since the demo has no burst-day concurrency to model.

    Returns (verdict, matched_receipt_id) where verdict is one of:
      "new" | "same_trip_duplicate_declined_silent" | "cross_report_duplicate_flagged"
    """
    h = receipt.get("sha256")
    trip_id = receipt.get("trip_id")
    prior = seen_hashes.get(h)
    if prior is None:
        seen_hashes[h] = {"receipt_id": receipt["receipt_id"], "trip_id": trip_id}
        return "new", None
    if prior["trip_id"] == trip_id:
        return "same_trip_duplicate_declined_silent", prior["receipt_id"]
    return "cross_report_duplicate_flagged", prior["receipt_id"]


def run_pre_gates(receipt: dict, cfg: PinnedConfig, seen_hashes: dict) -> PreGateResult:
    quality_ok, quality_reasons = check_image_quality(receipt, cfg)
    if not quality_ok:
        return PreGateResult(receipt["receipt_id"], False, "retake_request", quality_reasons)

    lang_ok, lang_reasons = check_language_currency(receipt, cfg)
    if not lang_ok:
        return PreGateResult(receipt["receipt_id"], False, "manual_intake_queue", lang_reasons)

    dup_verdict, matched_id = check_duplicate(receipt, seen_hashes, cfg)
    if dup_verdict == "same_trip_duplicate_declined_silent":
        return PreGateResult(receipt["receipt_id"], False, "declined_silent",
                              [f"matches {matched_id} in same trip"])
    if dup_verdict == "cross_report_duplicate_flagged":
        # Passes the gate (courtesy: withdraw-or-proceed offered to employee) but
        # is flagged for review downstream — recorded here, actioned in categorize.py.
        return PreGateResult(receipt["receipt_id"], True, "extraction",
                              [f"cross_report_duplicate_flagged: matches {matched_id}"])

    return PreGateResult(receipt["receipt_id"], True, "extraction", [])

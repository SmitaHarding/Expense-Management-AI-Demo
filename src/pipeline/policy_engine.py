"""Stage 6 — Policy check. Zero AI. This is the module PRD §11 means when it
says "these thresholds live in policy_config.json and are business decisions,
not code" — every number below is read from config, nothing is hard-coded.

Applies, in order:
  1. Non-reimbursable folio lines (minibar etc.) -> excluded, informational flag
  2. No-proof items -> excluded, informational flag
  3. Date verifiability check -> blocking flag if an item has no extractable
     date at all (CAT-07, v0.9.0); every date-dependent check below skips
     these items rather than guessing, since the blocking flag already
     forces manager review regardless of category or amount
  4. Travel-window check -> blocking flag if item date outside trip window
  5. Per diem: day-level auto-cap on meal_individual spend, aggregated across
     every *approved* report sharing a trip_id (PD-01; T10b v0.8.0)
  6. Team/client per-head caps: meal_group $40/head, client_entertainment
     $50/head — blocking, routes to S&TP regardless of report total
  6b. Accommodation per-night cap (CAT-08, v0.9.2): folio total / nights
     (employee-entered per hotel, else trip length) over $300/night —
     blocking, routes manager then S&TP; overage not auto-excluded
     (approvers decide)
  7. Supplemental-report check (T10b v0.8.0): a report for a trip that
     already has an approved sibling is flagged blocking regardless of
     amount, to discourage drip-fed partial submissions
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from .config_loader import PinnedConfig
from .validation import round_money


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def apply_non_reimbursable_and_proof_rules(items: list[dict], cfg: PinnedConfig) -> tuple[list[dict], list[dict]]:
    non_reimbursable_types = set(cfg.policy["non_reimbursable_line_types"])
    snippet_max = cfg.policy["capture"]["no_receipt_snippet_max_usd"]
    snippet_categories = set(cfg.policy["capture"]["snippet_allowed_categories"])
    snippet_limit = cfg.policy["capture"]["snippet_items_per_report"]

    out_items = []
    flags = []
    snippet_uses = 0

    for item in items:
        item = dict(item)
        line_type = item.get("line_type")
        proof = item.get("proof", {"type": "receipt"})

        if line_type in non_reimbursable_types:
            item["status"] = "excluded_non_reimbursable"
            flags.append({"flag": "non_reimbursable_item", "item_id": item["item_id"],
                           "detail": f"line_type '{line_type}' is non-reimbursable (folio exclusion)"})
        elif proof.get("type") == "none":
            item["status"] = "excluded_no_proof"
            flags.append({"flag": "excluded_no_proof", "item_id": item["item_id"],
                           "detail": "no receipt or qualifying card snippet; excluded from reimbursement"})
        elif proof.get("type") == "card_snippet":
            if item["category"] in snippet_categories and item["amount"] <= snippet_max and snippet_uses < snippet_limit:
                snippet_uses += 1
                item["status"] = item.get("status", "included")
            else:
                item["status"] = "excluded_no_proof"
                flags.append({"flag": "excluded_no_proof", "item_id": item["item_id"],
                               "detail": "card snippet not eligible (category/amount/limit) — treated as no proof"})
        else:
            item["status"] = item.get("status", "included")

        out_items.append(item)

    return out_items, flags


def apply_date_verifiability_check(items: list[dict]) -> list[dict]:
    """CAT-07 (v0.9.0): fires `date_unverifiable` for any included item whose
    date could not be extracted at all (both OCR and LLM returned None, or
    the LLM explicitly returned null per its extraction-prompt instruction
    to do so rather than guess). Applies uniformly across every category —
    a no-date meal receipt is exactly as unverifiable as a no-date hotel
    folio. Blocking, so it forces manager review on its own regardless of
    amount; every date-dependent check downstream (travel window, per diem
    day-bucketing) skips these items rather than guessing a date, since the
    review is already guaranteed."""
    flags = []
    for item in items:
        if item["status"] == "included" and item.get("date") is None:
            flags.append({"flag": "date_unverifiable", "item_id": item["item_id"],
                          "detail": "no date could be extracted from this receipt (neither OCR nor the "
                                    "vision model found one) — cannot verify travel-window or per-diem-day "
                                    "placement; routed to manager review"})
    return flags


def apply_travel_window_check(items: list[dict], trip: dict, cfg: PinnedConfig) -> list[dict]:
    """Items with no date (see apply_date_verifiability_check) are skipped
    here rather than evaluated — we have no date to compare against the
    window, and date_unverifiable already forces review on its own. This is
    a deliberate skip, not a pass: we are not claiming the item IS inside
    the travel window, only that we cannot check, and don't want to raise a
    second, misleading flag on top of the first."""
    grace = timedelta(days=cfg.policy["per_diem"]["travel_window_grace_days"])
    start = _parse_date(trip["start_date"]) - grace
    end = _parse_date(trip["end_date"]) + grace
    flags = []
    for item in items:
        if item["status"] not in ("included",):
            continue
        if item.get("date") is None:
            continue
        item_date = _parse_date(item["date"])
        if not (start <= item_date <= end):
            flags.append({"flag": "outside_travel_window", "item_id": item["item_id"],
                          "detail": f"item date {item_date} outside trip window {start}..{end}"})
    return flags


def apply_per_diem_cap(items: list[dict], cfg: PinnedConfig,
                        prior_approved_totals: dict | None = None,
                        prior_approved_from_reports: dict | None = None) -> tuple[dict, list[dict], Decimal]:
    """PD-01: day-level auto-cap on meal_individual spend only — travel items
    and team/client meals (separately capped) never count against it.

    T10b (v0.8.0): `prior_approved_totals` is a {date: amount} mapping,
    scoped to this report's trip, of meal totals already reimbursed by
    *other* reports for the same trip/day. `prior_approved_from_reports` is
    the parallel {date: [{report_id, amount_usd}, ...]} audit trail — which
    specific sibling report(s) contributed. The caller (orchestrator.py)
    slices both from a cross-report registry before calling — this function
    stays trip-agnostic and just consumes whatever it's handed. Only
    *approved* reports ever contribute to that registry (confirmed decision,
    2026-07-16): a sibling report still sitting in human review must never
    suppress another report's allowance, since it might still be rejected or
    withdrawn. Both default to empty, which reproduces the original
    single-report behavior exactly — every fixture that predates T10b is
    unaffected."""
    allowance = round_money(cfg.policy["per_diem"]["meals_incidentals_usd"], cfg)
    prior_approved_totals = prior_approved_totals or {}
    prior_approved_from_reports = prior_approved_from_reports or {}
    by_day = defaultdict(list)
    for item in items:
        if item["status"] == "included" and item["category"] == "meal_individual" and item.get("date") is not None:
            by_day[item["date"]].append(item)
        # date is None -> date_unverifiable already forces review; we can't
        # assign the item to a calendar day, so it sits outside the per-diem
        # cap rather than being silently mixed into a guessed day's total.

    per_diem_position = {}
    flags = []
    total_excluded_overage = Decimal("0")

    for day, day_items in sorted(by_day.items()):
        day_total = sum((round_money(i["amount"], cfg) for i in day_items), start=Decimal("0"))
        prior_approved = round_money(prior_approved_totals.get(day, 0.0), cfg)
        remaining_allowance = max(Decimal("0"), allowance - prior_approved)
        reimbursed = min(day_total, remaining_allowance)
        excess = day_total - reimbursed
        per_diem_position[day] = {
            "meal_total_usd": float(day_total),
            "allowance_usd": float(allowance),
            "prior_approved_usd": float(prior_approved),
            "prior_approved_from_reports": list(prior_approved_from_reports.get(day, [])),
            "reimbursed_usd": float(reimbursed),
            "excluded_overage_usd": float(excess),
        }
        if excess > 0:
            total_excluded_overage += excess
            if prior_approved > 0:
                detail = (f"{day}: meals ${day_total} (this report) + ${prior_approved} (already approved on "
                          f"other reports for this trip) exceed ${allowance} allowance; ${reimbursed} reimbursed "
                          f"this report, ${excess} excluded (informational — already enforced)")
            else:
                detail = (f"{day}: meals ${day_total} exceed ${allowance} allowance; "
                          f"${reimbursed} reimbursed, ${excess} excluded (informational — already enforced)")
            flags.append({"flag": "over_per_diem", "item_id": None, "detail": detail})

    return per_diem_position, flags, total_excluded_overage


def apply_supplemental_report_check(has_approved_sibling: bool) -> list[dict]:
    """T10b (v0.8.0): if this report's trip already has at least one approved
    sibling report on file, this report is a supplemental/partial submission
    — flag it blocking regardless of whether the per-diem cap is actually
    breached (the trigger is the existence of the supplemental submission,
    not the dollar amount). The company still pays what's owed — per-diem
    aggregation above already handles that — but a human (manager-level,
    standard blocking-flag routing, confirmed 2026-07-16) reviews it rather
    than letting it auto-approve, to discourage drip-fed partial reporting.
    A report that is the *first* one seen for its trip never gets this flag,
    even if a later sibling arrives for the same trip."""
    if not has_approved_sibling:
        return []
    return [{
        "flag": "supplemental_report_after_trip_approval",
        "item_id": None,
        "detail": "this trip already has at least one approved report on file; supplemental "
                   "submissions for an already-approved trip are routed to manager review "
                   "regardless of amount, to discourage drip-fed partial reporting",
    }]


def apply_team_client_caps(items: list[dict], cfg: PinnedConfig) -> list[dict]:
    """meal_group -> $40/head (internal only); client_entertainment -> $50/head
    (all attendees, external and internal). Both blocking -> S&TP regardless
    of report total (APR-04)."""
    caps = cfg.policy["team_caps"]
    flags = []
    for item in items:
        if item["status"] != "included":
            continue
        category = item["category"]
        attendees = item.get("attendees", [])
        if category == "meal_group" and attendees:
            per_head = round_money(item["amount"], cfg) / len(attendees)
            cap = Decimal(str(caps["meal_group_per_head_usd"]))
            if per_head > cap:
                flags.append({"flag": "over_team_cap", "item_id": item["item_id"],
                              "detail": f"${per_head}/head over ${cap}/head cap ({len(attendees)} attendees)"})
        elif category == "client_entertainment" and attendees:
            per_head = round_money(item["amount"], cfg) / len(attendees)
            cap = Decimal(str(caps["client_entertainment_per_head_usd"]))
            if per_head > cap:
                flags.append({"flag": "over_client_cap", "item_id": item["item_id"],
                              "detail": f"${per_head}/head over ${cap}/head cap ({len(attendees)} attendees, all counted)"})
    return flags


def _accommodation_nights(item: dict) -> tuple[int, str]:
    """Nights divisor for the CAT-08 per-night cap (v2, v0.9.6). Returns
    (nights, source).

    Nights is DERIVED from the folio's own check-in / check-out dates — the
    orchestrator writes item['check_in'] / item['check_out'] only when BOTH
    extraction paths agree on the stay range, so by the time policy runs these
    are trusted folio reads, not a self-reported number the employee can inflate.

    Precedence:
      1. Both check_in and check_out present and parseable, and nights >= 1
         -> nights = check_out - check_in (source 'folio').
      2. Otherwise -> conservative nights = 1 (source 'conservative', D1a):
         the folio dates couldn't be established or the two readers disagreed,
         so the cap is evaluated at the smallest plausible divisor. This never
         silently clears the cap — a genuinely over-cap folio still routes to a
         human, who verifies the true dates against the receipt image.

    The employee-entered `nights` field is deliberately NOT consulted (v2):
    it was the gaming lever this change removes."""
    ci, co = item.get("check_in"), item.get("check_out")
    if ci and co:
        try:
            n = (_parse_date(co) - _parse_date(ci)).days
        except (ValueError, TypeError):
            n = 0
        if n >= 1:
            return n, "folio"
    return 1, "conservative"


def apply_accommodation_cap(items: list[dict], trip: dict, cfg: PinnedConfig) -> list[dict]:
    """CAT-08 v2 (v0.9.6): a hotel folio's per-night cost must not exceed
    accommodation_cap.per_night_usd ($300 incl taxes). per-night = folio total /
    nights, where nights is DERIVED from the folio's check-in/check-out read by
    both paths (see _accommodation_nights) — no longer the employee-entered value.
    When the folio dates can't be established or the readers disagreed, the divisor
    falls to a conservative 1 night (D1a), so uncertainty errs toward review, never
    toward silently clearing the cap. Over the cap raises the BLOCKING flag
    over_accommodation_cap, which routes manager THEN S&TP via special_approval_flags.
    Unlike the per-diem, the overage is NOT auto-excluded — the full amount stands
    and a human decides, since accommodation overages are often legitimate (sold-out
    conference city). Mirrors apply_team_client_caps."""
    cap = Decimal(str(cfg.policy["accommodation_cap"]["per_night_usd"]))
    flags = []
    for item in items:
        if item["status"] != "included" or item["category"] != "accommodation":
            continue
        nights, src = _accommodation_nights(item)
        per_night = round_money(item["amount"], cfg) / nights
        if per_night > cap:
            flags.append({"flag": "over_accommodation_cap", "item_id": item["item_id"],
                          "detail": f"${per_night}/night (${round_money(item['amount'], cfg)} over "
                                    f"{nights} night(s), {src}) over ${cap}/night cap"})
    return flags


def apply_policy(items: list[dict], trip: dict, cfg: PinnedConfig,
                  prior_approved_totals: dict | None = None,
                  prior_approved_from_reports: dict | None = None,
                  has_approved_sibling: bool = False) -> dict:
    items, proof_flags = apply_non_reimbursable_and_proof_rules(items, cfg)
    date_flags = apply_date_verifiability_check(items)
    window_flags = apply_travel_window_check(items, trip, cfg)
    per_diem_position, per_diem_flags, excluded_overage = apply_per_diem_cap(
        items, cfg, prior_approved_totals, prior_approved_from_reports
    )
    cap_flags = apply_team_client_caps(items, cfg)
    accommodation_flags = apply_accommodation_cap(items, trip, cfg)
    supplemental_flags = apply_supplemental_report_check(has_approved_sibling)

    all_flags = proof_flags + date_flags + window_flags + per_diem_flags + cap_flags + accommodation_flags + supplemental_flags

    included_total = sum(
        (round_money(i["amount"], cfg) for i in items if i["status"] == "included"),
        start=Decimal("0"),
    ) - excluded_overage
    excluded_total = sum(
        (round_money(i["amount"], cfg) for i in items if i["status"] != "included"),
        start=Decimal("0"),
    ) + excluded_overage

    return {
        "items": items,
        "flags": all_flags,
        "per_diem_position": per_diem_position,
        "totals": {"included_usd": float(included_total), "excluded_usd": float(excluded_total)},
    }

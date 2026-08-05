"""Stage 8 — Route. Zero AI, zero discretion. Mirrors PRD §6 stage 4:
approval is a financial decision, so it is never made by a model. This
module only ever reads config and flags.

Four independent locks must ALL pass for auto-approval (PRD §6 stage 6):
  1. report_total < auto_approve_report_max_usd
  2. rolling_30d_total <= auto_approve_rolling_30d_max_usd
  3. zero blocking-tier flags present
  4. anomaly_score < anomaly_score_threshold

`over_team_cap` / `over_client_cap` force S&TP onto the route regardless of
report total (APR-04) — these are checked independently of the tiered matrix.
"""
from __future__ import annotations

from .config_loader import PinnedConfig


def determine_route(
    report_total: float,
    flags: list[dict],
    rolling_30d_total: float,
    anomaly_score: float,
    cfg: PinnedConfig,
) -> dict:
    present_flags = {f["flag"] for f in flags}
    blocking_present = present_flags & cfg.blocking_flags()
    has_blocking = bool(blocking_present)

    guards = {
        "report_under_auto_ceiling": report_total < cfg.policy["auto_approval"]["auto_approve_report_max_usd"],
        "rolling_30d_within_limit": rolling_30d_total <= cfg.policy["auto_approval"]["auto_approve_rolling_30d_max_usd"],
        "zero_blocking_flags": not has_blocking,
        "anomaly_below_threshold": anomaly_score < cfg.policy["auto_approval"]["anomaly_score_threshold"],
    }
    all_guards_pass = all(guards.values())

    reasons = []
    if all_guards_pass:
        route = ["auto"]
        reasons.append("all four auto-approval guards passed")
    else:
        matrix = cfg.policy["approval_matrix"]
        tier = next(t for t in matrix if t["max_report_total_usd"] is None or report_total <= t["max_report_total_usd"])
        route = list(tier["flagged_route"] if has_blocking else tier["clean_route"])
        if route == ["auto"]:
            # Guard failed for a reason other than a blocking flag (rolling 30d
            # or anomaly score) — falls back to manager, never silently auto-approves.
            route = ["manager"]
            reasons.append("in auto-approval band by amount, but a non-flag guard failed -> manager")
        else:
            failed = [k for k, v in guards.items() if not v]
            reasons.append(f"routed by approval_matrix tier (max_total={tier['max_report_total_usd']}); "
                            f"failed guards: {failed}")

    # Special-approval flags override regardless of total (APR-04).
    special = cfg.policy["special_approval_flags"]
    for flag_name, extra_roles in special.items():
        if flag_name in present_flags:
            route = [r for r in route if r != "auto"]
            for role in extra_roles:
                if role not in route:
                    route.append(role)
            reasons.append(f"'{flag_name}' present -> {extra_roles} added regardless of report total (APR-04)")

    return {"route": route, "guards": guards, "blocking_flags": sorted(blocking_present), "reasons": reasons}

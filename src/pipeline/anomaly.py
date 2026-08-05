"""Stage 7 — Anomaly score. Deterministic weighted scorecard, NOT machine
learning — a poorly-trained ML model would be worse than a transparent
scorecard (opaque *and* wrong), and there isn't enough labeled data yet to
train one anyway. Every signal's contribution is computed and returned so
an auditor can see exactly why a report scored what it scored — this is
the whole point of choosing a scorecard over an opaque model for v1.

Cold start (AUD-01c): employees with fewer than `min_observation_reports`
prior reports get neutral (zero) history-dependent signals — a new hire's
first report is not "anomalous" just because there's no history yet.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from statistics import mean, pstdev

from .config_loader import PinnedConfig
from .validation import round_money

HISTORY_DEPENDENT_SIGNALS = {"employee_baseline_deviation", "peer_group_deviation", "merchant_novelty"}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _is_round_number(amount: float) -> bool:
    cents = round(amount * 100)
    return cents % 500 == 0  # divisible by $5.00 — a crude but real signal


def compute_anomaly_score(
    report_total: float,
    items: list[dict],
    cfg: PinnedConfig,
    employee_history_totals: list[float] | None = None,
    peer_group_avg: float | None = None,
    employee_known_merchants: set[str] | None = None,
) -> tuple[float, dict]:
    weights = cfg.policy["anomaly_signals"]
    cold_start = cfg.policy["anomaly_cold_start"]
    min_obs = cold_start["min_observation_reports"]
    neutral_signals = set(cold_start["history_signals_neutral_below_min"])

    employee_history_totals = employee_history_totals or []
    employee_known_merchants = employee_known_merchants or set()
    n_prior_reports = len(employee_history_totals)
    is_cold_start = n_prior_reports < min_obs

    factors: dict[str, float] = {}
    auto_cap = cfg.policy["auto_approval"]["auto_approve_report_max_usd"]
    per_diem_cap = cfg.policy["per_diem"]["meals_incidentals_usd"]

    # -- employee_baseline_deviation --
    if "employee_baseline_deviation" in neutral_signals and is_cold_start:
        factors["employee_baseline_deviation"] = 0.0
    elif employee_history_totals:
        avg = mean(employee_history_totals)
        std = pstdev(employee_history_totals) or 1.0
        z = abs(report_total - avg) / std
        factors["employee_baseline_deviation"] = weights["employee_baseline_deviation"] * _clamp01(z / 3)
    else:
        factors["employee_baseline_deviation"] = 0.0

    # -- peer_group_deviation --
    if "peer_group_deviation" in neutral_signals and is_cold_start:
        factors["peer_group_deviation"] = 0.0
    elif peer_group_avg:
        ratio = report_total / peer_group_avg if peer_group_avg else 1.0
        factors["peer_group_deviation"] = weights["peer_group_deviation"] * _clamp01(abs(ratio - 1))
    else:
        factors["peer_group_deviation"] = 0.0

    # -- merchant_novelty --
    merchants = {i.get("merchant", "") for i in items}
    novel = merchants - employee_known_merchants
    novelty_ratio = len(novel) / len(merchants) if merchants else 0.0
    if "merchant_novelty" in neutral_signals and is_cold_start:
        factors["merchant_novelty"] = 0.0
    else:
        factors["merchant_novelty"] = weights["merchant_novelty"] * novelty_ratio

    # -- round_number_bias --
    round_items = [i for i in items if _is_round_number(i["amount"])]
    round_ratio = len(round_items) / len(items) if items else 0.0
    factors["round_number_bias"] = weights["round_number_bias"] * round_ratio

    # -- threshold_proximity -- how close to auto-approve ceiling or per-diem cap, from below
    prox_auto = 1 - _clamp01(abs(auto_cap - report_total) / auto_cap) if report_total <= auto_cap else 0.0
    factors["threshold_proximity"] = weights["threshold_proximity"] * prox_auto

    # -- snippet_usage_rate --
    snippet_items = [i for i in items if i.get("proof", {}).get("type") == "card_snippet"]
    snippet_ratio = len(snippet_items) / len(items) if items else 0.0
    factors["snippet_usage_rate"] = weights["snippet_usage_rate"] * snippet_ratio

    # -- weekend_holiday_claims -- items with no date (CAT-07, date_unverifiable)
    # are skipped here too, same reasoning as the travel-window check: we
    # can't verify weekend/weekday any more than we can verify anything else
    # about a date that doesn't exist, so they're excluded from this signal's
    # denominator entirely rather than guessed.
    dated_items = [i for i in items if i.get("date") is not None]
    weekend_items = [i for i in dated_items if date.fromisoformat(i["date"]).weekday() >= 5]
    weekend_ratio = len(weekend_items) / len(dated_items) if dated_items else 0.0
    factors["weekend_holiday_claims"] = weights["weekend_holiday_claims"] * weekend_ratio

    # -- submission_velocity -- not modeled in this demo (needs multi-report
    # timing history the fixture corpus doesn't carry); explicitly zeroed and
    # labeled rather than faked.
    factors["submission_velocity"] = 0.0

    score = _clamp01(sum(factors.values()))
    return score, factors

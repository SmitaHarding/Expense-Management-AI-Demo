"""Stage 3 — Validate. Zero AI, pure computation. Mirrors EXT-04/05/05b/08.

Two independent checks, either of which can force human review:
  1. Dual-path agreement: do OCR and LLM agree on amount/date/merchant?
  2. Arithmetic: do line items (incl. negative discount lines) sum to the
     receipt total? No confidence score can override a failed arithmetic
     check — this is a hard rule.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from .config_loader import PinnedConfig


def round_money(value: float, cfg: PinnedConfig) -> Decimal:
    dp = cfg.policy["rounding"]["currency_dp"]
    quant = Decimal(10) ** -dp
    return Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)


# Currencies whose conventional format uses comma as the decimal separator and
# dot/space as the thousands grouping — the inverse of US convention. Minimal by
# design (the only non-USD corpus is EUR); extend as real locales are added.
# Used ONLY as a tie-breaker: an unambiguous money pattern (both separators
# present, or a single separator with exactly two trailing digits) is resolved
# WITHOUT consulting currency, so a EUR receipt that prints "966.00" is never
# misread as 96600.
_COMMA_DECIMAL_CURRENCIES = {"EUR"}


def normalize_amount(raw, currency: str | None = None) -> tuple[Optional[float], Optional[str]]:
    """EXT-05a (T4): turn a messy amount — currency symbols, thousands
    separators, EU decimal commas, stray trailing characters like '105.00A' —
    into a numeric value, returning (value, amount_raw) where amount_raw is the
    ORIGINAL string preserved verbatim for audit. Sign is preserved for
    discount/refund lines (leading '-' or accounting parentheses). Returns
    (None, raw) when no number can be parsed.

    Separator resolution (currency is only the last resort):
      * both '.' and ',' present -> the LAST one is the decimal, the other is
        thousands (universally true for well-formed US "1,234.56" and EU
        "1.234,56"); currency not needed.
      * one separator, exactly 2 trailing digits -> decimal ("1,50", "966.00").
      * one separator, exactly 3 trailing digits -> thousands ("1,500"->1500).
      * separator repeats ("1.234.567") -> thousands.
      * anything left genuinely ambiguous -> fall back to the currency
        convention.
    """
    if raw is None:
        return None, None
    original = str(raw)
    # Already-numeric input (the LLM path returns JSON numbers): trust it, just
    # record the raw string form.
    if isinstance(raw, (int, float)):
        return float(raw), original
    s = original.strip()
    if not s:
        return None, original
    negative = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    cleaned = re.sub(r"[^0-9.,]", "", s)
    if not re.search(r"\d", cleaned):
        return None, original
    has_dot, has_comma = "." in cleaned, "," in cleaned

    if has_dot and has_comma:
        if cleaned.rfind(",") > cleaned.rfind("."):
            num = cleaned.replace(".", "").replace(",", ".")   # EU: 1.234,56
        else:
            num = cleaned.replace(",", "")                     # US: 1,234.56
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        count = cleaned.count(sep)
        tail = cleaned.rsplit(sep, 1)[1]
        if count == 1 and len(tail) == 2:
            num = cleaned.replace(sep, ".")     # money decimal: "1,50" / "966.00"
        elif count == 1 and len(tail) == 3:
            num = cleaned.replace(sep, "")       # thousands: "1,500" / "1.500" -> 1500
        elif count > 1:
            num = cleaned.replace(sep, "")       # repeated grouping: "1.234.567"
        elif (currency or "").upper() in _COMMA_DECIMAL_CURRENCIES:
            num = cleaned.replace(sep, ".") if sep == "," else cleaned.replace(sep, "")
        else:
            num = cleaned.replace(sep, "") if sep == "," else cleaned
    else:
        num = cleaned

    try:
        value = float(num)
    except ValueError:
        return None, original
    if negative and value > 0:
        value = -value
    return value, original


def fuzzy_merchant_match(a: str, b: str, min_ratio: float) -> bool:
    """Cheap fuzzy match (no external deps): normalized exact match, or one
    string contains the other after casefolding/stripping punctuation."""
    import re

    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.casefold())
    na, nb = norm(a), norm(b)
    if na == nb:
        return True
    if not na or not nb:
        return False
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    return shorter in longer and len(shorter) / len(longer) >= min_ratio


def check_dual_path_agreement(llm_item: dict, ocr_item: dict, cfg: PinnedConfig) -> tuple[bool, list[str]]:
    fields = cfg.policy["extraction"]["dual_extract_agreement_fields"]
    mismatches = []

    if "amount" in fields:
        if round_money(llm_item["amount"], cfg) != round_money(ocr_item["amount"], cfg):
            mismatches.append(f"amount: llm={llm_item['amount']} ocr={ocr_item['amount']}")
    if "date" in fields:
        if llm_item.get("date") != ocr_item.get("date"):
            mismatches.append(f"date: llm={llm_item.get('date')} ocr={ocr_item.get('date')}")
    if "merchant" in fields:
        min_ratio = cfg.policy["extraction"]["merchant_fuzzy_match_min"]
        if not fuzzy_merchant_match(llm_item.get("merchant", ""), ocr_item.get("merchant", ""), min_ratio):
            mismatches.append(f"merchant: llm={llm_item.get('merchant')!r} ocr={ocr_item.get('merchant')!r}")

    return (len(mismatches) == 0, mismatches)


def check_folio_stay_range(llm_item: dict, ocr_item: dict) -> tuple[bool, bool, str]:
    """CAT-08 v2 (v0.9.6): hotel folios carry check_in/check_out on BOTH
    extraction paths. This decides whether the stay range can be trusted to
    derive the per-night cap divisor. Returns (trusted, conflict, detail):

      - trusted=True   both readers supply both endpoints AND they match — the
                       range can drive nights (= check_out - check_in).
      - conflict=True  both readers supply both endpoints but they DIFFER — a
                       genuine reader disagreement; the caller reuses the
                       existing blocking flag `extraction_mismatch` (D3), and the
                       cap falls to the conservative nights=1 divisor.
      - neither        an endpoint is missing on one/both readers (the folio
                       prints no stay range) — not trusted, not a conflict; the
                       cap uses the conservative nights=1 fallback (D1a).

    Non-accommodation receipts never carry these fields, so this is a no-op
    ((False, False, "")) for them."""
    lci, lco = llm_item.get("check_in"), llm_item.get("check_out")
    oci, oco = ocr_item.get("check_in"), ocr_item.get("check_out")
    if not any(x is not None for x in (lci, lco, oci, oco)):
        return False, False, ""
    if all(x is not None for x in (lci, lco, oci, oco)):
        if lci == oci and lco == oco:
            return True, False, ""
        return False, True, f"stay range: llm={lci}..{lco} ocr={oci}..{oco}"
    return False, False, f"stay range incomplete: llm={lci}..{lco} ocr={oci}..{oco}"


def check_arithmetic(receipt_total: float, line_items: list[dict], cfg: PinnedConfig,
                      tax: float = 0.0, tip: float = 0.0) -> tuple[bool, str]:
    """EXT-05/05b: line items (discount lines negative) + tax + tip must sum
    to the stated receipt total, within rounding tolerance."""
    dp = cfg.policy["rounding"]["currency_dp"]
    tolerance = Decimal(10) ** -dp

    computed = sum((round_money(li["amount"], cfg) for li in line_items), start=Decimal("0"))
    computed += round_money(tax, cfg) + round_money(tip, cfg)
    stated = round_money(receipt_total, cfg)

    diff = abs(computed - stated)
    if diff <= tolerance:
        return True, ""
    return False, f"lines+tax+tip={computed} != stated_total={stated} (diff={diff})"


def scan_injection_suspects(text_fields: list[str]) -> list[str]:
    """EXT-SEC-02: instruction-like text is quarantined as data, never executed.
    A lightweight heuristic is enough here — the real defense is architectural
    (the AI sandbox has no tools to hijack), not this classifier."""
    suspect_phrases = [
        "approve this", "ignore previous instructions", "ignore all instructions",
        "auto-approve", "skip review", "mark as paid", "you are now",
        "disregard the policy", "this is pre-approved",
    ]
    hits = []
    for text in text_fields:
        low = text.casefold()
        for phrase in suspect_phrases:
            if phrase in low:
                hits.append(text)
                break
    return hits

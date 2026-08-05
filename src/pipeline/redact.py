"""Stage 0 — Redact (CAP-SEC-05). Zero AI, pure pattern matching + Luhn
validation. Mirrors ai_architecture.md v1.6 "Screen & scrub" trust zone.

Confirmed build requirement (2026-07-14 gap analysis, multiline_extraction_
gap_analysis.md): this module must run, and be proven by test to run,
before any receipt text reaches an extractor call. See orchestrator.py's
`_scrub_receipt` and tests/test_redact.py's ordering test.

Known, documented v1 scope limit (confirmed decision, 2026-07-14 session):
this scrubber operates on TEXT only (OCR output, structured fixture fields).
It cannot and does not redact PII baked into the pixels of a real receipt
photo. Track B's real Claude vision call (extraction.py::ClaudeExtractor.
extract_image) still sends the raw, unmodified image -- any PII visible in
the photo itself reaches the model. This is a deliberate scope boundary,
not an oversight: true pre-LLM image redaction (detecting and blurring PII
regions in a photo) is a different engineering problem (computer vision /
image manipulation) than pattern-matching text, was never priced in the
Part 2 effort estimate, and would sit in tension with EXT-04a's requirement
that the LLM read the raw image directly (so its errors stay independent
of OCR's). Revisit as a named v2 item if this pipeline goes past demo/pilot
scope. See README.md "Known limitations" and ai_architecture.md §4.

Scope (confirmed decisions DR1-DR5, GR9, D-DOB-correction):
  - Card PAN: Luhn-validated, masked to strict last-4 (DR5), including
    over-masked inputs (e.g. BIN+last4 -> last4 only).
  - Cardholder name adjacent to a masked/PAN card line: redacted as one
    linked pattern, not relying on standalone full-name detection (GR9).
  - SSN / EIN / ITIN: well-defined formats, built cheaply despite zero
    real evidence in the 31-receipt corpus (DR1).
  - Salutations / gender markers (Mr/Mrs/Ms/Mx/Dr + structural fields):
    same rationale as SSN (DR1).
  - Home address: bounded, label-anchored heuristic only (DR2) -- NOT
    general free-text address detection. Looks for the multi-line block
    following a small set of structural anchor labels seen in the real
    corpus ("NAME AND ADDRESS:", "Renter Information", etc.).
  - Date of birth: label-anchored, with document-type disambiguation.
    "DOB" on a rental-agreement-style document (has a driver's-license
    number and/or address block nearby) is treated as date of birth.
    "DOB" on an ordinary receipt with no such context is treated as date
    of billing and left alone -- this is the confirmed correction from
    review, not a bare label match (see Citycar vs. Pizzakitchen test
    pair in tests/test_redact.py).
  - Driver's license number: added despite not being on the original
    CAP-SEC-05 list, because real evidence (Citycar) put one right next
    to a DOB on the same document (DR3).
  - Explicitly OUT of scope for v1: bank account numbers, digital wallet
    IDs (DR1, deferred to v2 -- zero real evidence, high false-positive
    risk against reference/authorization numbers). Non-customer
    individuals -- merchant employees, drivers, store contacts -- are
    explicitly EXCLUDED from redaction scope (DR4); this scrubber targets
    only the traveling employee's own PII.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class RedactionResult:
    redacted_text: str
    counts: dict  # {"credit_card_pan": 1, "cardholder_name": 1, ...} -- zero-valued keys omitted
    applied: bool  # True iff this text was actually passed through the scrubber


# ---------------------------------------------------------------------------
# Card PAN + Luhn + linked cardholder name (DR5, GR9)
# ---------------------------------------------------------------------------

# Matches masked-or-full PAN-shaped runs: groups of digits/X's, 12-19 chars
# total once separators are stripped, e.g. "424242XXXXXX4242", "4242 4242
# 4242 4242", "XXXXXXXXXXXX2961".
_PAN_RE = re.compile(r"\b(?:[0-9Xx]{4}[ -]?){3,4}[0-9Xx]{1,4}\b")
# A name sitting on the same line as (or the line immediately after) a
# masked/PAN card line, in "LAST/FIRST", "LAST, FIRST", or "FIRST LAST"
# shapes -- this is the GR9 linked pattern, not standalone name detection.
# The all-caps fallback is deliberately restricted to ALL-CAPS words only
# (matching how real POS payment/signature lines actually print names --
# "DOE/JANE", "JANE DOE") rather than any two capitalized words, which
# would false-positive on ordinary mixed-case phrases like "Order Number".
_NAME_NEAR_CARD_RE = re.compile(
    r"\b([A-Z][A-Za-z'\-]+)\s*[/,]\s*([A-Z][A-Za-z'\-]+)\b"  # DOE/JANE or DOE, JANE
    r"|\b([A-Z]{2,20})\s+([A-Z]{2,20})\b"  # JANE DOE (all-caps signature style)
)
# A run of masked digits (a PAN the receipt already prints starred/X'd out).
# This anchors linked-name redaction even when the PAN's last 4 are OCR-garbled
# into letters so _PAN_RE can't match — the case that leaked "JENS WALTER" on a
# real receipt (T8/E6, 2026-07-23): the card number was pre-masked, the last 4
# read as noise, so no card line was recognized and the signature name survived.
_MASKED_PAN_RE = re.compile(r"[X*]{8,}")
# A SHORT network-labeled masked card, e.g. "Mc *4702", "VISA *1234",
# "MASTERCARD ****0908". Hotel folios and some POS slips print only the network
# name + a starred last-4 (fewer than the 8 mask chars _MASKED_PAN_RE needs and
# too few digit groups for _PAN_RE), so neither of the other anchors fires and
# the signature name below it survives (the hilton_1/hilton_2 leak, 2026-07-28).
# A mask char (* # x) is REQUIRED so this can't match a network label next to a
# plain number that isn't a card.
_SHORT_MASKED_PAN_RE = re.compile(
    r"(?i)\b(?:visa|master\s?card|mastercard|mc|amex|american\s?express|discover|disc)\b"
    r"[ .:\-]*[*#xX]+\s*\d{2,4}\b"
)
# Cardholder-agreement / signature boilerplate. The signature block (and the
# printed cardholder name) clusters right around this text, so it's a second
# anchor for linked-name redaction independent of where the PAN prints.
# "member name" is included for the card-slip / folio layout that labels the
# cardholder line "CARD MEMBER NAME" (the name prints just below it).
_CARD_CONTEXT_RE = re.compile(
    r"(?i)card\s*issuer|cardholder|card\s*member|cardmember|member\s*name|"
    r"agree(?:s|d)?\s+to\s+pay|according\s+to\s+card|signature"
)
# All-caps two-word phrases that print in/near signature blocks but are NOT
# names — never redact these even when they sit inside the anchor window.
_NAME_STOPWORDS = {
    "STORE COPY", "CUSTOMER COPY", "MERCHANT COPY", "GUEST COPY",
    "CARDHOLDER COPY", "CARD ISSUER", "CARD MEMBER", "THANK YOU",
    "AMOUNT DUE", "TOTAL DUE", "GRAND TOTAL", "BALANCE DUE", "APPROVAL CODE",
    "AUTH CODE", "CHANGE DUE", "TOTAL SALE",
}


def _luhn_valid(digits: str) -> bool:
    if len(digits) < 12 or not digits.isdigit():
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _redact_card_and_name(text: str, counts: dict) -> str:
    lines = text.splitlines()
    card_line_idx = set()

    def _mask_pan(m: re.Match, _line_idx: int, _genuine_hits: list) -> str:
        raw = m.group(0)
        digits_only = re.sub(r"[^0-9]", "", raw)
        has_mask_char = "X" in raw.upper()
        # Only treat as a card if it's mask-shaped already, OR the digit
        # portion Luhn-validates as a real PAN -- avoids false-positiving on
        # arbitrary long digit runs (order numbers, phone numbers). Note:
        # re.subn() counts a regex MATCH, not a genuine redaction, so a
        # separate list is used to record only real hits -- otherwise a
        # PAN-shaped-but-Luhn-invalid digit run (order number) would still
        # mark this line as a "card line" and trigger linked-name redaction
        # on an unrelated line (found via test_luhn_rejects_non_card_digit_runs).
        if not has_mask_char and not _luhn_valid(digits_only):
            return raw
        counts["credit_card_pan"] = counts.get("credit_card_pan", 0) + 1
        _genuine_hits.append(_line_idx)
        last4 = digits_only[-4:] if len(digits_only) >= 4 else digits_only
        return f"XXXXXXXXXXXX{last4}"  # strict last-4 only (DR5) -- no BIN, even if the input had one

    masked_line_idx = set()   # PAN already printed masked (may be OCR-garbled)
    context_idx = set()       # cardholder-agreement / signature boilerplate
    for i, line in enumerate(lines):
        genuine_hits: list = []
        new_line = _PAN_RE.sub(lambda m, _i=i, _h=genuine_hits: _mask_pan(m, _i, _h), line)
        if genuine_hits:
            card_line_idx.add(i)
            lines[i] = new_line
        if _MASKED_PAN_RE.search(line) or _SHORT_MASKED_PAN_RE.search(line):
            masked_line_idx.add(i)
        if _CARD_CONTEXT_RE.search(line):
            context_idx.add(i)

    # Linked name redaction: GR9's "linked pattern," not free-standing name
    # detection anywhere in the document (which would over-redact merchant
    # names, city names, etc.). Anchored on three signals:
    #   - a genuine card line, or the line right after it;
    #   - a masked-PAN line (pre-starred number), or the line right after it;
    #   - a cardholder-agreement / signature line, plus the few lines ABOVE it
    #     (the printed name/signature sits just above that boilerplate).
    anchor_lines = card_line_idx | masked_line_idx
    candidate_idx = set()
    for i in anchor_lines:
        candidate_idx.add(i)
        if i + 1 < len(lines):
            candidate_idx.add(i + 1)
    # Cardholder-agreement / signature anchoring — but ONLY at or below the
    # first card line. The merchant name prints in the header ABOVE the card
    # number; the cardholder name prints in the signature block at/below it.
    # This keeps a wide window for recall (a name a few lines from the
    # boilerplate) without eating the merchant name (a real extracted field,
    # not PII) — the regression the pizzakitchen golden case guards against.
    if anchor_lines:
        first_anchor = min(anchor_lines)
        for i in context_idx:
            for j in range(i - 3, i + 2):
                if first_anchor <= j < len(lines):
                    candidate_idx.add(j)

    for i in candidate_idx:
        def _mask_name(m: re.Match, _i=i) -> str:
            if m.group(0).strip().upper() in _NAME_STOPWORDS:
                return m.group(0)  # a signature-block phrase, not a person's name
            counts["cardholder_name"] = counts.get("cardholder_name", 0) + 1
            return "[REDACTED_NAME]"
        lines[i] = _NAME_NEAR_CARD_RE.sub(_mask_name, lines[i])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SSN / EIN / ITIN (DR1)
# ---------------------------------------------------------------------------
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")           # 123-45-6789
_EIN_RE = re.compile(r"\b\d{2}-\d{7}\b")                  # 12-3456789
_ITIN_RE = re.compile(r"\b9\d{2}-\d{2}-\d{4}\b")          # ITINs start with 9; matches SSN shape


def _redact_ssn_ein_itin(text: str, counts: dict) -> str:
    def _mask_itin(m):
        counts["itin"] = counts.get("itin", 0) + 1
        return "[REDACTED_ITIN]"

    def _mask_ssn(m):
        counts["ssn"] = counts.get("ssn", 0) + 1
        return "[REDACTED_SSN]"

    def _mask_ein(m):
        counts["ein"] = counts.get("ein", 0) + 1
        return "[REDACTED_EIN]"

    text = _ITIN_RE.sub(_mask_itin, text)
    text = _SSN_RE.sub(_mask_ssn, text)
    text = _EIN_RE.sub(_mask_ein, text)
    return text


# ---------------------------------------------------------------------------
# Salutations / gender markers (DR1)
# ---------------------------------------------------------------------------
_SALUTATION_RE = re.compile(r"\b(Mr|Mrs|Ms|Mx|Dr)\.?\s+[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)?\b")
_GENDER_FIELD_RE = re.compile(r"\b(Gender|Sex)\s*:\s*(Male|Female|M|F|X)\b", re.IGNORECASE)


def _redact_salutation(text: str, counts: dict) -> str:
    def _mask_sal(m):
        counts["salutation"] = counts.get("salutation", 0) + 1
        return "[REDACTED_NAME]"

    def _mask_gender(m):
        counts["gender_marker"] = counts.get("gender_marker", 0) + 1
        return f"{m.group(1)}: [REDACTED]"

    text = _SALUTATION_RE.sub(_mask_sal, text)
    text = _GENDER_FIELD_RE.sub(_mask_gender, text)
    return text


# ---------------------------------------------------------------------------
# Home address: bounded, label-anchored heuristic only (DR2)
# ---------------------------------------------------------------------------
_ADDRESS_ANCHOR_RE = re.compile(
    r"(?im)^(.*\b(NAME AND ADDRESS|RENTER INFORMATION|ADDRESS|BILLING ADDRESS)\s*:?\s*)$"
)


def _redact_address(text: str, counts: dict) -> str:
    """Redacts the 1-2 non-blank lines immediately following a small,
    known set of structural anchor labels -- not general free-text address
    detection (CAP-SEC-05 specifies pattern matching, not NLP).

    Real documents sometimes stack more than one anchor label back-to-back
    (e.g. "Renter Information:" directly above "NAME AND ADDRESS:") --
    skip past any consecutive anchor-matching lines first, so the actual
    address block below them is what gets collected, not the next anchor
    label itself (found via the citycar_full_pii_rental_agreement test)."""
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        if _ADDRESS_ANCHOR_RE.match(lines[i]):
            j = i + 1
            while j < len(lines) and _ADDRESS_ANCHOR_RE.match(lines[j]):
                out.append(lines[j])
                j += 1
            block_start = j
            redacted_any = False
            while j < len(lines) and j < block_start + 2 and lines[j].strip():
                out.append("[REDACTED_ADDRESS]" if not redacted_any else lines[j])
                # Redact the first non-blank line fully; second line (city/
                # postal) redacted as part of the same block, not double
                # counted as a second address hit.
                if not redacted_any:
                    counts["home_address"] = counts.get("home_address", 0) + 1
                    redacted_any = True
                else:
                    out[-1] = "[REDACTED_ADDRESS_CONT]"
                j += 1
            i = j
            continue
        i += 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Date of birth vs. date of billing (DOB correction from review) + driver's
# license number (DR3)
# ---------------------------------------------------------------------------
_DOB_LABEL_RE = re.compile(r"(?im)\bDOB\s*:?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})")
_DL_RE = re.compile(r"(?im)\b(?:DL|DRIVER'?S?\s*LICEN[SC]E)\s*(?:NO\.?|NUMBER|#)?\s*:?\s*([A-Za-z0-9]{6,15})\b")

# Context signals that this document is a rental-agreement-style document
# where "DOB" almost certainly means date of birth, not date of billing.
_BIRTH_CONTEXT_RE = re.compile(
    r"(?i)driver'?s?\s*licen[sc]e|renter information|rental agreement|name and address"
)


def _is_birth_context(text: str) -> bool:
    return bool(_BIRTH_CONTEXT_RE.search(text))


def _redact_dob(text: str, counts: dict) -> str:
    if not _is_birth_context(text):
        return text  # ordinary receipt -- "DOB" here is date of billing, leave alone

    def _mask_dob(m):
        counts["date_of_birth"] = counts.get("date_of_birth", 0) + 1
        return "DOB: [REDACTED_DOB]"

    return _DOB_LABEL_RE.sub(_mask_dob, text)


# ---------------------------------------------------------------------------
# Customer phone/email (gap found during Phase 1 build, not in the original
# task list -- see note below)
# ---------------------------------------------------------------------------
# NOTE: the gap analysis's evidence table (GR "Phone / email (individual)")
# found real customer phone/email on the Citycar rental agreement, and DR4
# resolved the NON-customer exclusion question -- but no task in the Phase 1
# breakdown (1.1-1.11) or the Part 2 effort table ever actually budgeted a
# phone/email pattern for the customer. Rather than leave a traveling
# employee's own phone/email unprotected while their DOB and address two
# lines away get redacted, this adds it cheaply, gated by the same
# rental-agreement/labeled-identity-block context as DOB -- so it naturally
# stays out of ordinary receipts (Subway's employee footer, an unrelated
# person, is never in this context) without needing to re-litigate DR4.
# Flagged to the user as an addition beyond the literal task list.
_PHONE_RE = re.compile(r"\+?\d[\d\- ]{8,14}\d")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[A-Za-z]{2,}\b")


def _redact_customer_contact(text: str, counts: dict) -> str:
    if not _is_birth_context(text):
        return text  # not a rental-agreement-style identity block -- leave any phone/email alone (DR4)

    def _mask_email(m):
        counts["email"] = counts.get("email", 0) + 1
        return "[REDACTED_EMAIL]"

    def _mask_phone(m):
        counts["phone"] = counts.get("phone", 0) + 1
        return "[REDACTED_PHONE]"

    text = _EMAIL_RE.sub(_mask_email, text)
    text = _PHONE_RE.sub(_mask_phone, text)
    return text


def _redact_drivers_license(text: str, counts: dict) -> str:
    def _mask_dl(m):
        counts["drivers_license_number"] = counts.get("drivers_license_number", 0) + 1
        return f"{m.group(0).split(m.group(1))[0]}[REDACTED_DL]"

    return _DL_RE.sub(_mask_dl, text)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def redact_text(text: str | None) -> RedactionResult:
    """Runs every confirmed-in-scope detector over `text` and returns the
    redacted text plus per-type counts. Order matters: card+name first
    (frees the PAN regex from later passes matching its own digits), then
    the independent-format detectors, then the two context-dependent ones.

    Deterministic, no AI, no external calls -- safe to run on every receipt
    regardless of downstream extractor choice (mock or real)."""
    if not text:
        return RedactionResult(redacted_text=text or "", counts={}, applied=True)

    counts: dict = {}
    out = text
    out = _redact_card_and_name(out, counts)
    out = _redact_ssn_ein_itin(out, counts)
    out = _redact_salutation(out, counts)
    out = _redact_address(out, counts)
    out = _redact_drivers_license(out, counts)  # before DOB: DL number sits near DOB, keep them independent
    out = _redact_dob(out, counts)
    out = _redact_customer_contact(out, counts)

    return RedactionResult(redacted_text=out, counts=counts, applied=True)


def redaction_summary(result: RedactionResult) -> dict:
    """Shape matching expense_report.schema.json's `processing.pii_redaction`
    field: counts/types only, never redacted values (schema description,
    enforced structurally by not including redacted_text in this output)."""
    return {"applied": result.applied, "redaction_counts": result.counts}

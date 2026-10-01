from __future__ import annotations

from dataclasses import dataclass

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat


@dataclass
class PhoneCheckResult:
    is_valid: bool
    e164: str = ""
    reason: str = ""


def validate_phone(raw: str, default_region: str = "PK") -> PhoneCheckResult:
    """
    Parse and validate a phone number. Returns E.164 when valid.

    Google Sheets often strips the leading '+', so bare international digits
    like 923221723864 are tried as +923221723864 before region-local parse.
    """
    text = (raw or "").strip()
    if not text:
        return PhoneCheckResult(False, reason="Phone is empty")
    if text.upper() in {"#ERROR!", "#N/A", "#VALUE!", "#REF!"}:
        return PhoneCheckResult(False, reason="Phone cell is an error value")

    digits = "".join(ch for ch in text if ch.isdigit())
    candidates: list[tuple[str, str | None]] = []

    # 1) Prefer explicit international form (handles sheet-stripped '+').
    if digits and not text.startswith("+"):
        candidates.append((f"+{digits}", None))
    if text.startswith("+"):
        candidates.append((text, None))
    # 2) As stored, with default region (UK/US national numbers).
    candidates.append((text, default_region or None))
    if digits and digits != text:
        candidates.append((digits, default_region or None))

    last_reason = "Number is not possible"
    seen: set[str] = set()
    for candidate, region in candidates:
        key = f"{candidate}|{region}"
        if key in seen:
            continue
        seen.add(key)
        try:
            parsed = phonenumbers.parse(candidate, region)
        except NumberParseException as exc:
            last_reason = f"Parse error: {exc}"
            continue

        if not phonenumbers.is_possible_number(parsed):
            last_reason = "Number is not possible"
            continue
        if not phonenumbers.is_valid_number(parsed):
            last_reason = "Number is not a valid phone number"
            continue

        e164 = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
        return PhoneCheckResult(True, e164=e164)

    return PhoneCheckResult(False, reason=last_reason)

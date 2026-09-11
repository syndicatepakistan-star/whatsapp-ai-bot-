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
    """Parse and validate a phone number. Returns E.164 when valid."""
    text = (raw or "").strip()
    if not text:
        return PhoneCheckResult(False, reason="Phone is empty")

    try:
        parsed = phonenumbers.parse(text, default_region or None)
    except NumberParseException as exc:
        return PhoneCheckResult(False, reason=f"Parse error: {exc}")

    if not phonenumbers.is_possible_number(parsed):
        return PhoneCheckResult(False, reason="Number is not possible")
    if not phonenumbers.is_valid_number(parsed):
        return PhoneCheckResult(False, reason="Number is not a valid phone number")

    e164 = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
    return PhoneCheckResult(True, e164=e164)

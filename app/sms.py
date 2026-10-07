"""UK SMS via Vonage Messages API (manual follow-up / lead captured)."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

VONAGE_MESSAGES_URL = "https://api.nexmo.com/v1/messages"


@dataclass
class SmsSendResult:
    ok: bool
    detail: str = ""
    message_id: str = ""
    skipped: bool = False


def is_uk_e164(e164_phone: str) -> bool:
    """True when E.164 is a UK number (+44…)."""
    digits = "".join(ch for ch in (e164_phone or "") if ch.isdigit())
    return digits.startswith("44") and len(digits) >= 12


def _first_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        return "there"
    return text.split()[0]


def _digits_for_vonage(e164_phone: str) -> str:
    """Vonage `to` / numeric `from` expect digits only (no '+')."""
    return "".join(ch for ch in (e164_phone or "") if ch.isdigit())


def already_sms_noted(notes: str) -> bool:
    lower = (notes or "").lower()
    return "sms sent" in lower or "sms:" in lower


class SmsClient:
    """
    Send SMS with the same copy as WhatsApp templates:
    - Not Completed → WHAPI_QUIZ_FOLLOWUP_MESSAGE_TEXT (+ quiz URL)
    - Completed → WHAPI_MESSAGE_TEXT (+ intake URL)
    One message body (URL inline) — SMS has no rich link-preview split.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def configured(self) -> bool:
        return bool(
            (self.settings.vonage_api_key or "").strip()
            and (self.settings.vonage_api_secret or "").strip()
            and (self.settings.vonage_from or "").strip()
            and self.settings.sms_enabled
        )

    def _auth_header(self) -> str:
        raw = (
            f"{self.settings.vonage_api_key.strip()}:"
            f"{self.settings.vonage_api_secret.strip()}"
        )
        token = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    def build_quiz_body(
        self,
        *,
        name: str,
        email: str = "",
        quiz_url: str = "",
    ) -> str:
        display_name = _first_name(name)
        display_email = (email or "").strip().lower()
        display_url = (quiz_url or "").strip()
        template = self.settings.whapi_quiz_followup_message_text or (
            "Hi {name},\n\nPlease complete Syn Diagnosis:\n{quiz_url}\n\n"
            "With Honour\nThe Syndicate"
        )
        template = template.replace("\\n", "\n")
        body = (
            template.replace("{name}", display_name)
            .replace("{email}", display_email)
            .replace("{quiz_url}", display_url)
            .replace("{intake_url}", display_url)
        ).strip()
        if (
            display_url
            and "{quiz_url}" not in template
            and "{intake_url}" not in template
            and display_url not in body
        ):
            body = f"{body}\n\n{display_url}"
        while "\n\n\n" in body:
            body = body.replace("\n\n\n", "\n\n")
        return body.strip()

    def build_audit_body(
        self,
        *,
        name: str,
        email: str = "",
        intake_url: str = "",
    ) -> str:
        display_name = _first_name(name)
        display_email = (email or "").strip().lower()
        display_url = (intake_url or "").strip()
        template = self.settings.whapi_message_text or (
            "Hi {name},\n\nJust complete the quick form.\n\nWith Honour\nThe Syndicate"
        )
        template = template.replace("\\n", "\n")
        body = (
            template.replace("{name}", display_name)
            .replace("{email}", display_email)
            .replace("{intake_url}", display_url)
        ).strip()
        if display_url and "{intake_url}" not in template and display_url not in body:
            body = f"{body}\n\n{display_url}"
        while "\n\n\n" in body:
            body = body.replace("\n\n\n", "\n\n")
        return body.strip()

    def send_text(self, *, e164_phone: str, body: str) -> SmsSendResult:
        if not self.configured():
            return SmsSendResult(
                False,
                detail="sms_not_configured",
                skipped=True,
            )
        if not is_uk_e164(e164_phone):
            return SmsSendResult(False, detail="not_uk_number", skipped=True)

        to_digits = _digits_for_vonage(e164_phone)
        from_raw = (self.settings.vonage_from or "").strip()
        # Numeric / E.164 → digits only; alphanumeric brand IDs stay as-is.
        if from_raw.startswith("+") or from_raw.isdigit():
            from_value = _digits_for_vonage(from_raw)
        else:
            from_value = from_raw

        text = (body or "").strip()
        if not text:
            return SmsSendResult(False, detail="empty_body")

        payload = {
            "message_type": "text",
            "text": text,
            "to": to_digits,
            "from": from_value,
            "channel": "sms",
        }
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    VONAGE_MESSAGES_URL,
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            logger.exception("Vonage SMS request failed to=%s", to_digits)
            return SmsSendResult(False, detail=f"sms_request_error: {exc}")

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}

        if response.status_code >= 400:
            logger.warning(
                "Vonage SMS error status=%s to=%s body=%s",
                response.status_code,
                to_digits,
                data,
            )
            return SmsSendResult(False, detail=str(data)[:400])

        message_id = ""
        if isinstance(data, dict):
            message_id = str(
                data.get("message_uuid")
                or data.get("message_id")
                or data.get("uuid")
                or ""
            )
        logger.info("Vonage SMS sent to=%s id=%s", to_digits, message_id or "-")
        return SmsSendResult(True, detail="sms_sent", message_id=message_id)

    def send_for_diagnosis(
        self,
        *,
        e164_phone: str,
        name: str,
        email: str,
        diagnosis: str,
        quiz_url: str = "",
        intake_url: str = "",
    ) -> SmsSendResult:
        """
        diagnosis Completed → audit/intake SMS
        otherwise → Syn Diagnosis quiz continue SMS
        """
        if (diagnosis or "").strip().lower() == "completed":
            body = self.build_audit_body(
                name=name,
                email=email,
                intake_url=intake_url,
            )
        else:
            body = self.build_quiz_body(
                name=name,
                email=email,
                quiz_url=quiz_url or intake_url,
            )
        return self.send_text(e164_phone=e164_phone, body=body)


def quiz_resume_url(settings: Settings) -> str:
    configured = (settings.quiz_resume_url or "").strip()
    if configured:
        return configured
    base = (settings.intake_base_url or "https://the-syndicate.com").rstrip("/")
    return f"{base}/quiz/questions"


def format_sms_notes_suffix(result: SmsSendResult) -> str:
    if result.skipped and result.detail in {"sms_not_configured", "not_uk_number"}:
        return ""
    if result.ok:
        mid = result.message_id or result.detail or "ok"
        return f"SMS sent: {mid}"
    return f"SMS failed: {(result.detail or 'error')[:180]}"

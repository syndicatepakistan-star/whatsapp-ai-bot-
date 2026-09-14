from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class WhatsAppCheckResult:
    exists: bool
    detail: str = ""


@dataclass
class WhatsAppSendResult:
    ok: bool
    detail: str = ""
    message_id: str = ""


def _digits_only(e164_phone: str) -> str:
    """Whapi expects digits only (country code + number), no '+'."""
    return "".join(ch for ch in (e164_phone or "") if ch.isdigit())


class WhatsAppClient:
    """Whapi.Cloud client: check contact + send text."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _configured(self) -> bool:
        return bool((self.settings.whapi_token or "").strip())

    def _base_url(self) -> str:
        return (self.settings.whapi_api_url or "https://gate.whapi.cloud").rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.whapi_token.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def check_number_on_whatsapp(self, e164_phone: str) -> WhatsAppCheckResult:
        """
        Check if number is on WhatsApp via Whapi:
        POST /contacts  { contacts: ["92300..."], blocking: "wait", force_check: true }
        status "valid" => exists, "invalid" => does not.
        """
        if not self._configured():
            # Fail closed: do not pretend the number exists / try to send.
            logger.error(
                "WHAPI_TOKEN is missing on Railway. "
                "Set WHAPI_TOKEN then redeploy. No WhatsApp message will be sent."
            )
            return WhatsAppCheckResult(False, detail="whapi_token_missing")

        phone = _digits_only(e164_phone)
        if not phone:
            return WhatsAppCheckResult(False, detail="empty_phone")

        url = f"{self._base_url()}/contacts"
        payload = {
            "blocking": "wait",
            "force_check": True,
            "contacts": [phone],
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            logger.exception("Whapi contacts check failed")
            return WhatsAppCheckResult(False, detail=f"contacts_request_error: {exc}")

        if response.status_code >= 400:
            logger.warning(
                "Whapi contacts API error %s: %s",
                response.status_code,
                response.text,
            )
            return WhatsAppCheckResult(
                False,
                detail=f"contacts_api_error_{response.status_code}",
            )

        data = response.json()
        contacts = data.get("contacts") or []
        if not contacts:
            return WhatsAppCheckResult(False, detail="no_contact_result")

        status = (contacts[0].get("status") or "").lower()
        exists = status == "valid"
        return WhatsAppCheckResult(exists, detail=status or "unknown")

    def send_text(
        self,
        *,
        e164_phone: str,
        name: str,
        email: str = "",
        intake_url: str = "",
    ) -> WhatsAppSendResult:
        """Send a text message via Whapi: POST /messages/text"""
        if not self._configured():
            return WhatsAppSendResult(False, detail="whapi_not_configured")

        phone = _digits_only(e164_phone)
        if not phone:
            return WhatsAppSendResult(False, detail="empty_phone")

        display_name = (name or "there").strip() or "there"
        display_email = (email or "").strip().lower()
        display_url = (intake_url or "").strip()

        template = self.settings.whapi_message_text or (
            "Hi {name}, your Syn Diagnosis follow-up is ready.\n"
            "Open your form here: {intake_url}"
        )
        body = (
            template.replace("{name}", display_name)
            .replace("{email}", display_email)
            .replace("{intake_url}", display_url or display_email)
        )

        url = f"{self._base_url()}/messages/text"
        payload = {
            "to": phone,
            "body": body,
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            logger.exception("Whapi send failed")
            return WhatsAppSendResult(False, detail=f"send_request_error: {exc}")

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}

        if response.status_code >= 400:
            logger.warning("Whapi send error %s: %s", response.status_code, data)
            return WhatsAppSendResult(False, detail=str(data))

        message_id = ""
        if isinstance(data, dict):
            message_id = str(
                data.get("message", {}).get("id")
                or data.get("id")
                or data.get("message_id")
                or ""
            )

        return WhatsAppSendResult(True, detail="sent", message_id=message_id)

    # Backwards-compatible alias used by older workflow code
    def send_template(
        self,
        *,
        e164_phone: str,
        name: str,
        email: str = "",
        intake_url: str = "",
    ) -> WhatsAppSendResult:
        return self.send_text(
            e164_phone=e164_phone,
            name=name,
            email=email,
            intake_url=intake_url,
        )

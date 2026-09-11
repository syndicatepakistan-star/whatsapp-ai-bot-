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


class WhatsAppClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _configured(self) -> bool:
        return bool(
            self.settings.whatsapp_token
            and self.settings.whatsapp_phone_number_id
        )

    def _base_url(self) -> str:
        return (
            f"https://graph.facebook.com/{self.settings.whatsapp_api_version}"
            f"/{self.settings.whatsapp_phone_number_id}"
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.whatsapp_token}",
            "Content-Type": "application/json",
        }

    def check_number_on_whatsapp(self, e164_phone: str) -> WhatsAppCheckResult:
        """
        Check if a number is registered on WhatsApp via Meta contacts endpoint.
        Falls back to 'assume exists' only when API is not configured (dev mode).
        """
        if not self._configured():
            logger.warning("WhatsApp not configured; skipping existence check")
            return WhatsAppCheckResult(True, detail="whatsapp_not_configured_skip_check")

        url = f"{self._base_url()}/contacts"
        payload = {
            "blocking": "wait",
            "contacts": [e164_phone],
            "force_check": True,
        }

        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            logger.exception("WhatsApp contacts check failed")
            return WhatsAppCheckResult(False, detail=f"contacts_request_error: {exc}")

        if response.status_code >= 400:
            # Some WABA setups disallow contacts API. Treat as unknown → manual.
            logger.warning(
                "WhatsApp contacts API error %s: %s",
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
        # Meta returns "valid" when the number is on WhatsApp.
        exists = status == "valid"
        return WhatsAppCheckResult(exists, detail=status or "unknown")

    def send_template(
        self,
        *,
        e164_phone: str,
        name: str,
    ) -> WhatsAppSendResult:
        if not self._configured():
            return WhatsAppSendResult(False, detail="whatsapp_not_configured")

        if not self.settings.whatsapp_template_name:
            return WhatsAppSendResult(False, detail="whatsapp_template_name_missing")

        to = e164_phone.lstrip("+")
        url = f"{self._base_url()}/messages"
        template: dict = {
            "name": self.settings.whatsapp_template_name,
            "language": {"code": self.settings.whatsapp_template_language},
        }
        if self.settings.whatsapp_template_has_name_param:
            template["components"] = [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": name or "there"},
                    ],
                }
            ]

        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": template,
        }

        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.post(url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            logger.exception("WhatsApp send failed")
            return WhatsAppSendResult(False, detail=f"send_request_error: {exc}")

        body = {}
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}

        if response.status_code >= 400:
            logger.warning("WhatsApp send error %s: %s", response.status_code, body)
            return WhatsAppSendResult(False, detail=str(body))

        message_id = ""
        messages = body.get("messages") or []
        if messages:
            message_id = messages[0].get("id") or ""

        return WhatsAppSendResult(True, detail="sent", message_id=message_id)

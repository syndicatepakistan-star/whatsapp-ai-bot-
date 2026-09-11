from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import Settings
from app.phone_validator import validate_phone
from app.sheets import GoogleSheetsClient
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

STATUS_WRONG_NUMBER = "wrong number"
STATUS_MANUAL_NEEDED = "manual follow-up needed"
STATUS_WHATSAPP_SENT = "whatsapp template sent"
STATUS_WHATSAPP_SEND_FAILED = "whatsapp send failed"


@dataclass
class LeadResult:
    action: str
    status: str
    phone_e164: str = ""
    detail: str = ""


class LeadWorkflow:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sheets = GoogleSheetsClient(settings)
        self.whatsapp = WhatsAppClient(settings)

    def process(self, *, name: str, email: str, phone: str) -> LeadResult:
        name = (name or "").strip()
        email = (email or "").strip().lower()
        phone = (phone or "").strip()

        phone_check = validate_phone(phone, self.settings.default_phone_region)
        if not phone_check.is_valid:
            self._safe_sheet(
                name=name,
                email=email,
                phone=phone,
                status=STATUS_WRONG_NUMBER,
                notes=phone_check.reason,
            )
            return LeadResult(
                action="sheet_wrong_number",
                status=STATUS_WRONG_NUMBER,
                detail=phone_check.reason,
            )

        e164 = phone_check.e164
        wa_check = self.whatsapp.check_number_on_whatsapp(e164)
        if not wa_check.exists:
            self._safe_sheet(
                name=name,
                email=email,
                phone=e164,
                status=STATUS_MANUAL_NEEDED,
                notes=(
                    "WhatsApp number does not exist / could not verify. "
                    f"Detail: {wa_check.detail}. Text them manually."
                ),
            )
            return LeadResult(
                action="sheet_manual_followup",
                status=STATUS_MANUAL_NEEDED,
                phone_e164=e164,
                detail=wa_check.detail,
            )

        send = self.whatsapp.send_template(e164_phone=e164, name=name)
        if not send.ok:
            self._safe_sheet(
                name=name,
                email=email,
                phone=e164,
                status=STATUS_WHATSAPP_SEND_FAILED,
                notes=send.detail,
            )
            return LeadResult(
                action="sheet_send_failed",
                status=STATUS_WHATSAPP_SEND_FAILED,
                phone_e164=e164,
                detail=send.detail,
            )

        self._safe_sheet(
            name=name,
            email=email,
            phone=e164,
            status=STATUS_WHATSAPP_SENT,
            notes=send.message_id or "sent",
        )
        return LeadResult(
            action="whatsapp_sent",
            status=STATUS_WHATSAPP_SENT,
            phone_e164=e164,
            detail=send.message_id or "sent",
        )

    def _safe_sheet(
        self,
        *,
        name: str,
        email: str,
        phone: str,
        status: str,
        notes: str = "",
    ) -> None:
        try:
            self.sheets.append_lead(
                name=name,
                email=email,
                phone=phone,
                status=status,
                notes=notes,
            )
        except Exception:
            logger.exception("Failed to write Google Sheet status=%s", status)

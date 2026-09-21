from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

from app.community_agent import CommunityAgent
from app.config import Settings
from app.phone_validator import validate_phone
from app.sheets import GoogleSheetsClient
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

STATUS_WRONG_NUMBER = "wrong number"
STATUS_MANUAL_NEEDED = "manual follow-up needed"
STATUS_WHATSAPP_SENT = "whatsapp message sent"
STATUS_WHATSAPP_SEND_FAILED = "whatsapp send failed"


@dataclass
class LeadResult:
    action: str
    status: str
    phone_e164: str = ""
    detail: str = ""
    intake_url: str = ""
    group_add_status: str = ""
    group_add_detail: str = ""


def build_intake_url(*, email: str, intake_url: str, intake_base_url: str) -> str:
    """Prefer website-provided intake_url; otherwise build from email."""
    provided = (intake_url or "").strip()
    if provided:
        return provided

    email_norm = (email or "").strip().lower()
    if not email_norm:
        return ""

    base = (intake_base_url or "https://the-syndicate.com").rstrip("/")
    return f"{base}/quiz/intake?email={quote(email_norm, safe='')}"


class LeadWorkflow:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sheets = GoogleSheetsClient(settings)
        self.whatsapp = WhatsAppClient(settings)
        self.community = CommunityAgent(settings, self.whatsapp)

    def process(
        self,
        *,
        name: str,
        email: str,
        phone: str,
        intake_url: str = "",
    ) -> LeadResult:
        name = (name or "").strip()
        email = (email or "").strip().lower()
        phone = (phone or "").strip()
        resolved_intake_url = build_intake_url(
            email=email,
            intake_url=intake_url,
            intake_base_url=self.settings.intake_base_url,
        )

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
                intake_url=resolved_intake_url,
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
                    + (f" Intake: {resolved_intake_url}" if resolved_intake_url else "")
                ),
            )
            return LeadResult(
                action="sheet_manual_followup",
                status=STATUS_MANUAL_NEEDED,
                phone_e164=e164,
                detail=wa_check.detail,
                intake_url=resolved_intake_url,
            )

        send = self.whatsapp.send_text(
            e164_phone=e164,
            name=name,
            email=email,
            intake_url=resolved_intake_url,
        )
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
                intake_url=resolved_intake_url,
            )

        # Sub-agent A: direct-add to WhatsApp group (invite fallback if blocked).
        group_status = ""
        group_detail = ""
        try:
            community = self.community.add_lead_to_group(name=name, e164_phone=e164)
            group_status = community.status
            group_detail = community.detail
        except Exception as exc:
            logger.exception("Community agent failed phone=%s", e164)
            group_status = "failed"
            group_detail = f"exception: {exc}"

        self._safe_sheet(
            name=name,
            email=email,
            phone=e164,
            status=STATUS_WHATSAPP_SENT,
            notes=(send.message_id or "sent")
            + (f" | {resolved_intake_url}" if resolved_intake_url else ""),
            group_add_status=group_status,
            group_add_detail=group_detail,
        )
        return LeadResult(
            action="whatsapp_sent",
            status=STATUS_WHATSAPP_SENT,
            phone_e164=e164,
            detail=send.message_id or "sent",
            intake_url=resolved_intake_url,
            group_add_status=group_status,
            group_add_detail=group_detail,
        )

    def _safe_sheet(
        self,
        *,
        name: str,
        email: str,
        phone: str,
        status: str,
        notes: str = "",
        group_add_status: str = "",
        group_add_detail: str = "",
    ) -> None:
        try:
            self.sheets.append_lead(
                name=name,
                email=email,
                phone=phone,
                status=status,
                notes=notes,
                group_add_status=group_add_status,
                group_add_detail=group_add_detail,
            )
        except Exception:
            logger.exception("Failed to write Google Sheet status=%s", status)

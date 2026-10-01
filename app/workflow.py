"""Lead capture → wait for quiz → audit WhatsApp + group only when Completed."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

from app.community_agent import CommunityAgent
from app.config import Settings
from app.phone_validator import validate_phone
from app.sheets import GoogleSheetsClient, _find_diagnosis_matches
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

STATUS_WRONG_NUMBER = "wrong number"
STATUS_MANUAL_NEEDED = "manual follow-up needed"
STATUS_LEAD_CAPTURED = "lead captured"
STATUS_WHATSAPP_SENT = "whatsapp message sent"
STATUS_WHATSAPP_SEND_FAILED = "whatsapp send failed"
STATUS_DIAGNOSIS_UPDATED = "diagnosis updated"

AUDIT_ALREADY_SENT_STATUSES = {
    STATUS_WHATSAPP_SENT,
    "whatsapp_sent",
    "sent",
}


def _normalize_diagnosis(raw: str) -> str:
    value = (raw or "").strip()
    if value.lower() == "completed":
        return "Completed"
    return "Not Completed"


@dataclass
class LeadResult:
    action: str
    status: str
    phone_e164: str = ""
    detail: str = ""
    intake_url: str = ""
    group_add_status: str = ""
    group_add_detail: str = ""
    diagnosis: str = "Not Completed"


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
        diagnosis: str = "Not Completed",
        sheet_only: bool = False,
    ) -> LeadResult:
        name = (name or "").strip()
        email = (email or "").strip().lower()
        phone = (phone or "").strip()
        diagnosis_norm = _normalize_diagnosis(diagnosis)
        resolved_intake_url = build_intake_url(
            email=email,
            intake_url=intake_url,
            intake_base_url=self.settings.intake_base_url,
        )

        # Historical backfill: diagnosis column only — never WhatsApp / group.
        if sheet_only:
            return self._sheet_only_diagnosis(
                email=email,
                phone=phone,
                diagnosis=diagnosis_norm,
                intake_url=resolved_intake_url,
            )

        # Mid-quiz phone capture: sheet row only. Audit + group wait for Completed.
        if diagnosis_norm == "Not Completed":
            return self._capture_awaiting_quiz(
                name=name,
                email=email,
                phone=phone,
                intake_url=resolved_intake_url,
            )

        # Quiz finished: send audit WhatsApp + group/channel join immediately.
        return self._deliver_audit_on_completed(
            name=name,
            email=email,
            phone=phone,
            intake_url=resolved_intake_url,
        )

    def _sheet_only_diagnosis(
        self,
        *,
        email: str,
        phone: str,
        diagnosis: str,
        intake_url: str,
    ) -> LeadResult:
        updated = False
        try:
            updated = self.sheets.update_lead_diagnosis(
                email=email,
                phone=phone,
                diagnosis=diagnosis,
            )
        except Exception:
            logger.exception("Failed to update diagnosis on sheet email=%s", email)

        if updated:
            return LeadResult(
                action="sheet_diagnosis_updated",
                status=STATUS_DIAGNOSIS_UPDATED,
                phone_e164=phone,
                detail=f"diagnosis set to {diagnosis} on existing lead row",
                intake_url=intake_url,
                diagnosis=diagnosis,
            )
        return LeadResult(
            action="sheet_diagnosis_skipped",
            status=STATUS_DIAGNOSIS_UPDATED,
            phone_e164=phone,
            detail="no matching Leads row to update",
            intake_url=intake_url,
            diagnosis=diagnosis,
        )

    def _capture_awaiting_quiz(
        self,
        *,
        name: str,
        email: str,
        phone: str,
        intake_url: str,
    ) -> LeadResult:
        """
        First phone capture while quiz incomplete.
        Write/update Leads row only — no WhatsApp, no group add.
        Cron follow-up runs after QUIZ_FOLLOWUP_DELAY_MINUTES if still Not Completed.
        """
        matches = self._matching_rows(email=email, phone=phone)
        phone_check = validate_phone(phone, self.settings.default_phone_region)

        if not phone_check.is_valid:
            return self._write_or_update_capture(
                matches=matches,
                name=name,
                email=email,
                phone=phone,
                status=STATUS_WRONG_NUMBER,
                notes=phone_check.reason,
                group_add_status="skipped",
                group_add_detail="wrong_number",
                intake_url=intake_url,
                action="sheet_wrong_number",
                detail=phone_check.reason,
                phone_e164="",
            )

        e164 = phone_check.e164
        wa_check = self.whatsapp.check_number_on_whatsapp(e164)
        if not wa_check.exists:
            return self._write_or_update_capture(
                matches=matches,
                name=name,
                email=email,
                phone=e164,
                status=STATUS_MANUAL_NEEDED,
                notes=(
                    "WhatsApp number does not exist / could not verify. "
                    f"Detail: {wa_check.detail}. Text them manually."
                    + (f" Intake: {intake_url}" if intake_url else "")
                ),
                group_add_status="skipped",
                group_add_detail="not_on_whatsapp",
                intake_url=intake_url,
                action="sheet_manual_followup",
                detail=wa_check.detail,
                phone_e164=e164,
            )

        notes = (
            f"awaiting quiz completion | "
            f"{(self.settings.quiz_resume_url or '').strip() or (self.settings.intake_base_url.rstrip('/') + '/quiz/questions')}"
        )
        return self._write_or_update_capture(
            matches=matches,
            name=name,
            email=email,
            phone=e164,
            status=STATUS_LEAD_CAPTURED,
            notes=notes,
            group_add_status="",
            group_add_detail="",
            intake_url=intake_url,
            action="lead_captured",
            detail="awaiting quiz completion — no WhatsApp until Completed",
            phone_e164=e164,
        )

    def _write_or_update_capture(
        self,
        *,
        matches: list[dict],
        name: str,
        email: str,
        phone: str,
        status: str,
        notes: str,
        group_add_status: str,
        group_add_detail: str,
        intake_url: str,
        action: str,
        detail: str,
        phone_e164: str,
    ) -> LeadResult:
        if matches:
            for match in matches:
                # Never downgrade a row that already got the audit WhatsApp.
                existing_status = (match.get("status") or "").strip().lower()
                if existing_status in AUDIT_ALREADY_SENT_STATUSES:
                    try:
                        self.sheets.update_lead_diagnosis(
                            email=email,
                            phone=phone,
                            diagnosis="Not Completed",
                        )
                    except Exception:
                        logger.exception("Failed diagnosis touch on existing sent row")
                    continue
                try:
                    self.sheets.update_lead_fields(
                        int(match["_row"]),
                        name=name,
                        email=email,
                        phone=phone,
                        status=status,
                        notes=notes,
                        group_add_status=group_add_status,
                        group_add_detail=group_add_detail,
                        diagnosis="Not Completed",
                    )
                except Exception:
                    logger.exception("Failed to update lead capture row=%s", match.get("_row"))
            return LeadResult(
                action=action,
                status=status,
                phone_e164=phone_e164,
                detail=detail,
                intake_url=intake_url,
                group_add_status=group_add_status,
                group_add_detail=group_add_detail,
                diagnosis="Not Completed",
            )

        self._safe_sheet(
            name=name,
            email=email,
            phone=phone,
            status=status,
            notes=notes,
            group_add_status=group_add_status,
            group_add_detail=group_add_detail,
            diagnosis="Not Completed",
        )
        return LeadResult(
            action=action,
            status=status,
            phone_e164=phone_e164,
            detail=detail,
            intake_url=intake_url,
            group_add_status=group_add_status,
            group_add_detail=group_add_detail,
            diagnosis="Not Completed",
        )

    def _deliver_audit_on_completed(
        self,
        *,
        name: str,
        email: str,
        phone: str,
        intake_url: str,
    ) -> LeadResult:
        matches = self._matching_rows(email=email, phone=phone)

        # Mark diagnosis Completed on any matching rows first.
        try:
            self.sheets.update_lead_diagnosis(
                email=email,
                phone=phone,
                diagnosis="Completed",
            )
        except Exception:
            logger.exception("Failed to set Completed diagnosis email=%s", email)

        # Cancel pending quiz follow-up — they finished before/without needing it.
        for match in matches:
            follow = (match.get("quiz_followup_status") or "").strip().lower()
            if follow in {"", "pending"}:
                try:
                    self.sheets.update_lead_quiz_followup(
                        int(match["_row"]),
                        status="skipped",
                        detail="completed_before_followup",
                    )
                except Exception:
                    logger.exception("Failed to skip followup row=%s", match.get("_row"))

        already_sent = any(
            (m.get("status") or "").strip().lower() in AUDIT_ALREADY_SENT_STATUSES
            for m in matches
        )
        if already_sent:
            return LeadResult(
                action="sheet_diagnosis_updated",
                status=STATUS_DIAGNOSIS_UPDATED,
                phone_e164=phone,
                detail="diagnosis Completed — audit WhatsApp already sent earlier",
                intake_url=intake_url,
                diagnosis="Completed",
            )

        phone_check = validate_phone(phone, self.settings.default_phone_region)
        if not phone_check.is_valid:
            self._persist_completed_outcome(
                matches=matches,
                name=name,
                email=email,
                phone=phone,
                status=STATUS_WRONG_NUMBER,
                notes=phone_check.reason,
                group_add_status="skipped",
                group_add_detail="wrong_number",
            )
            return LeadResult(
                action="sheet_wrong_number",
                status=STATUS_WRONG_NUMBER,
                detail=phone_check.reason,
                intake_url=intake_url,
                group_add_status="skipped",
                group_add_detail="wrong_number",
                diagnosis="Completed",
            )

        e164 = phone_check.e164
        wa_check = self.whatsapp.check_number_on_whatsapp(e164)
        if not wa_check.exists:
            notes = (
                "WhatsApp number does not exist / could not verify. "
                f"Detail: {wa_check.detail}. Text them manually."
                + (f" Intake: {intake_url}" if intake_url else "")
            )
            self._persist_completed_outcome(
                matches=matches,
                name=name,
                email=email,
                phone=e164,
                status=STATUS_MANUAL_NEEDED,
                notes=notes,
                group_add_status="skipped",
                group_add_detail="not_on_whatsapp",
            )
            return LeadResult(
                action="sheet_manual_followup",
                status=STATUS_MANUAL_NEEDED,
                phone_e164=e164,
                detail=wa_check.detail,
                intake_url=intake_url,
                group_add_status="skipped",
                group_add_detail="not_on_whatsapp",
                diagnosis="Completed",
            )

        send = self.whatsapp.send_text(
            e164_phone=e164,
            name=name,
            email=email,
            intake_url=intake_url,
        )
        if not send.ok:
            self._persist_completed_outcome(
                matches=matches,
                name=name,
                email=email,
                phone=e164,
                status=STATUS_WHATSAPP_SEND_FAILED,
                notes=send.detail,
                group_add_status="skipped",
                group_add_detail="whatsapp_send_failed",
            )
            return LeadResult(
                action="sheet_send_failed",
                status=STATUS_WHATSAPP_SEND_FAILED,
                phone_e164=e164,
                detail=send.detail,
                intake_url=intake_url,
                group_add_status="skipped",
                group_add_detail="whatsapp_send_failed",
                diagnosis="Completed",
            )

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

        notes = (send.message_id or "sent") + (f" | {intake_url}" if intake_url else "")
        self._persist_completed_outcome(
            matches=matches,
            name=name,
            email=email,
            phone=e164,
            status=STATUS_WHATSAPP_SENT,
            notes=notes,
            group_add_status=group_status,
            group_add_detail=group_detail,
        )
        return LeadResult(
            action="whatsapp_sent",
            status=STATUS_WHATSAPP_SENT,
            phone_e164=e164,
            detail=send.message_id or "sent",
            intake_url=intake_url,
            group_add_status=group_status,
            group_add_detail=group_detail,
            diagnosis="Completed",
        )

    def _persist_completed_outcome(
        self,
        *,
        matches: list[dict],
        name: str,
        email: str,
        phone: str,
        status: str,
        notes: str,
        group_add_status: str,
        group_add_detail: str,
    ) -> None:
        if matches:
            for match in matches:
                try:
                    self.sheets.update_lead_fields(
                        int(match["_row"]),
                        name=name,
                        email=email,
                        phone=phone,
                        status=status,
                        notes=notes,
                        group_add_status=group_add_status,
                        group_add_detail=group_add_detail,
                        diagnosis="Completed",
                    )
                except Exception:
                    logger.exception("Failed to update completed lead row=%s", match.get("_row"))
            return

        self._safe_sheet(
            name=name,
            email=email,
            phone=phone,
            status=status,
            notes=notes,
            group_add_status=group_add_status,
            group_add_detail=group_add_detail,
            diagnosis="Completed",
        )

    def _matching_rows(self, *, email: str, phone: str) -> list[dict]:
        try:
            rows = self.sheets.list_lead_rows()
        except Exception:
            logger.exception("Failed to list lead rows for match")
            return []
        return _find_diagnosis_matches(rows, email=email, phone=phone)

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
        diagnosis: str = "Not Completed",
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
                diagnosis=diagnosis,
            )
        except Exception:
            logger.exception("Failed to write Google Sheet status=%s", status)

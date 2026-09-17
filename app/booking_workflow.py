"""Audit booking WhatsApp workflow: confirm message + sheet log + reminders."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import Settings
from app.phone_validator import validate_phone
from app.sheets import GoogleSheetsClient
from app.whatsapp import WhatsAppClient, WhatsAppSendResult

logger = logging.getLogger(__name__)

STATUS_WRONG_NUMBER = "wrong number"
STATUS_MANUAL_NEEDED = "manual follow-up needed"
STATUS_BOOKING_SENT = "booking whatsapp sent"
STATUS_BOOKING_SEND_FAILED = "booking whatsapp send failed"
STATUS_REMINDER_SENT = "reminder sent"
STATUS_REMINDER_FAILED = "reminder failed"


@dataclass
class BookingResult:
    action: str
    status: str
    phone_e164: str = ""
    detail: str = ""
    slot_local: str = ""


def _parse_iso(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_slot_local(slot_start: str, slot_end: str, tz_name: str) -> str:
    start = _parse_iso(slot_start)
    end = _parse_iso(slot_end)
    if not start:
        return slot_start or ""
    tz = None
    try:
        tz = ZoneInfo((tz_name or "Asia/Karachi").strip() or "Asia/Karachi")
    except Exception:
        try:
            tz = ZoneInfo("UTC")
        except Exception:
            tz = timezone.utc
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz) if end else None
    day = local_start.strftime("%A %d %B")
    t0 = local_start.strftime("%I:%M %p").lstrip("0")
    tz_label = getattr(tz, "key", None) or str(tz_name or "UTC")
    if local_end:
        t1 = local_end.strftime("%I:%M %p").lstrip("0")
        return f"{day} · {t0} – {t1} ({tz_label})"
    return f"{day} · {t0} ({tz_label})"


def render_booking_message(
    template: str,
    *,
    name: str,
    email: str,
    meet_link: str,
    slot_local: str,
    timezone_name: str,
) -> str:
    text = (template or "").replace("\\n", "\n")
    return (
        text.replace("{name}", (name or "there").strip() or "there")
        .replace("{email}", (email or "").strip().lower())
        .replace("{meet_link}", (meet_link or "").strip())
        .replace("{slot_local}", slot_local)
        .replace("{timezone}", (timezone_name or "").strip())
    ).strip()


class BookingWorkflow:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sheets = GoogleSheetsClient(settings)
        self.whatsapp = WhatsAppClient(settings)

    def process(
        self,
        *,
        name: str,
        email: str,
        phone: str,
        meet_link: str,
        slot_start: str,
        slot_end: str,
        timezone_name: str = "Asia/Karachi",
        booking_id: str = "",
    ) -> BookingResult:
        name = (name or "").strip()
        email = (email or "").strip().lower()
        phone = (phone or "").strip()
        meet_link = (meet_link or "").strip()
        timezone_name = (timezone_name or "Asia/Karachi").strip() or "Asia/Karachi"
        slot_local = format_slot_local(slot_start, slot_end, timezone_name)

        phone_check = validate_phone(phone, self.settings.default_phone_region)
        if not phone_check.is_valid:
            self._safe_sheet(
                name=name,
                email=email,
                phone=phone,
                status=STATUS_WRONG_NUMBER,
                notes=phone_check.reason,
                meet_link=meet_link,
                slot_start=slot_start,
                slot_end=slot_end,
                timezone_name=timezone_name,
                booking_id=booking_id,
                reminder_sent="n/a",
            )
            return BookingResult(
                action="sheet_wrong_number",
                status=STATUS_WRONG_NUMBER,
                detail=phone_check.reason,
                slot_local=slot_local,
            )

        e164 = phone_check.e164
        wa_check = self.whatsapp.check_number_on_whatsapp(e164)
        if not wa_check.exists:
            self._safe_sheet(
                name=name,
                email=email,
                phone=e164,
                status=STATUS_MANUAL_NEEDED,
                notes=f"WhatsApp check failed: {wa_check.detail}",
                meet_link=meet_link,
                slot_start=slot_start,
                slot_end=slot_end,
                timezone_name=timezone_name,
                booking_id=booking_id,
                reminder_sent="n/a",
            )
            return BookingResult(
                action="sheet_manual_followup",
                status=STATUS_MANUAL_NEEDED,
                phone_e164=e164,
                detail=wa_check.detail,
                slot_local=slot_local,
            )

        send = self._send_booking_message(
            e164_phone=e164,
            name=name,
            email=email,
            meet_link=meet_link,
            slot_local=slot_local,
            timezone_name=timezone_name,
            reminder=False,
        )
        if not send.ok:
            self._safe_sheet(
                name=name,
                email=email,
                phone=e164,
                status=STATUS_BOOKING_SEND_FAILED,
                notes=send.detail,
                meet_link=meet_link,
                slot_start=slot_start,
                slot_end=slot_end,
                timezone_name=timezone_name,
                booking_id=booking_id,
                reminder_sent="no",
            )
            return BookingResult(
                action="sheet_send_failed",
                status=STATUS_BOOKING_SEND_FAILED,
                phone_e164=e164,
                detail=send.detail,
                slot_local=slot_local,
            )

        self._safe_sheet(
            name=name,
            email=email,
            phone=e164,
            status=STATUS_BOOKING_SENT,
            notes=send.message_id or "sent",
            meet_link=meet_link,
            slot_start=slot_start,
            slot_end=slot_end,
            timezone_name=timezone_name,
            booking_id=booking_id,
            reminder_sent="no",
        )
        return BookingResult(
            action="whatsapp_sent",
            status=STATUS_BOOKING_SENT,
            phone_e164=e164,
            detail=send.message_id or "sent",
            slot_local=slot_local,
        )

    def process_due_reminders(self) -> dict:
        """
        Send reminders for bookings where:
        - reminder_sent == no
        - status was booking whatsapp sent
        - now is within [slot_start - reminder_minutes, slot_start)
        """
        minutes = int(self.settings.booking_reminder_minutes_before or 60)
        now = datetime.now(timezone.utc)
        window_start = now
        # Eligible if slot starts between now and now+minutes (i.e. within reminder window)
        window_end = now + timedelta(minutes=max(1, minutes))

        sent = 0
        failed = 0
        skipped = 0

        try:
            rows = self.sheets.list_booking_rows()
        except Exception:
            logger.exception("Failed to list booking rows for reminders")
            return {"ok": False, "error": "sheet_read_failed", "sent": 0, "failed": 0, "skipped": 0}

        for row in rows:
            reminder_flag = (row.get("reminder_sent") or "").strip().lower()
            status = (row.get("status") or "").strip().lower()
            if reminder_flag in {"yes", "n/a", "true", "1"}:
                skipped += 1
                continue
            if STATUS_BOOKING_SENT not in status and "whatsapp sent" not in status:
                # Only remind after successful confirm send
                if "sent" not in status:
                    skipped += 1
                    continue

            slot_start = (row.get("slot_start") or "").strip()
            start = _parse_iso(slot_start)
            if not start:
                skipped += 1
                continue
            # Due if start is in the future and within reminder window
            if not (window_start < start <= window_end):
                skipped += 1
                continue

            phone = (row.get("phone") or "").strip()
            name = (row.get("name") or "").strip()
            email = (row.get("email") or "").strip()
            meet_link = (row.get("meet_link") or "").strip()
            tz_name = (row.get("timezone") or "Asia/Karachi").strip()
            slot_end = (row.get("slot_end") or "").strip()
            slot_local = format_slot_local(slot_start, slot_end, tz_name)
            row_number = int(row.get("_row") or 0)

            send = self._send_booking_message(
                e164_phone=phone,
                name=name,
                email=email,
                meet_link=meet_link,
                slot_local=slot_local,
                timezone_name=tz_name,
                reminder=True,
            )
            if send.ok:
                sent += 1
                if row_number:
                    try:
                        self.sheets.mark_booking_reminder_sent(row_number)
                    except Exception:
                        logger.exception("Failed to mark reminder_sent row=%s", row_number)
            else:
                failed += 1
                logger.warning("Reminder send failed phone=%s detail=%s", phone, send.detail)

        return {"ok": True, "sent": sent, "failed": failed, "skipped": skipped}

    def _send_booking_message(
        self,
        *,
        e164_phone: str,
        name: str,
        email: str,
        meet_link: str,
        slot_local: str,
        timezone_name: str,
        reminder: bool,
    ) -> WhatsAppSendResult:
        template = (
            self.settings.whapi_booking_reminder_text
            if reminder
            else self.settings.whapi_booking_message_text
        )
        body = render_booking_message(
            template,
            name=name,
            email=email,
            meet_link=meet_link,
            slot_local=slot_local,
            timezone_name=timezone_name,
        )
        # Prefer link-preview for Meet URL when configured
        if self.settings.whapi_link_separate and meet_link:
            link_result = self.whatsapp._post_text(
                phone="".join(ch for ch in e164_phone if ch.isdigit()),
                body=meet_link,
            )
            if not link_result.ok:
                return link_result
            text_only = render_booking_message(
                template,
                name=name,
                email=email,
                meet_link="",
                slot_local=slot_local,
                timezone_name=timezone_name,
            )
            while "\n\n\n" in text_only:
                text_only = text_only.replace("\n\n\n", "\n\n")
            text_only = text_only.replace("Join with Google Meet:\n", "").replace("Join here:\n", "").strip()
            text_result = self.whatsapp._post_text(
                phone="".join(ch for ch in e164_phone if ch.isdigit()),
                body=text_only or body,
            )
            return text_result if text_result.ok else text_result

        return self.whatsapp._post_text(
            phone="".join(ch for ch in e164_phone if ch.isdigit()),
            body=body,
        )

    def _safe_sheet(
        self,
        *,
        name: str,
        email: str,
        phone: str,
        status: str,
        notes: str = "",
        meet_link: str = "",
        slot_start: str = "",
        slot_end: str = "",
        timezone_name: str = "",
        booking_id: str = "",
        reminder_sent: str = "no",
    ) -> None:
        try:
            self.sheets.append_booking(
                name=name,
                email=email,
                phone=phone,
                status=status,
                notes=notes,
                meet_link=meet_link,
                slot_start=slot_start,
                slot_end=slot_end,
                timezone_name=timezone_name,
                booking_id=booking_id,
                reminder_sent=reminder_sent,
            )
        except Exception:
            logger.exception("Failed to write booking sheet status=%s", status)

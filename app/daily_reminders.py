"""
Daily WhatsApp reminders for the quiz → audit funnel.

Rules:
  1) diagnosis Not Completed → quiz reminder (once per calendar day)
  2) diagnosis Completed + no Bookings row → audit booking reminder (once per day)
  3) audit booked → stop daily reminders (mark done)

Does not replace the 10-minute first quiz follow-up (/cron/quiz-followup).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings
from app.phone_validator import validate_phone
from app.sheets import GoogleSheetsClient, _digits_only, _normalize_email
from app.sms import quiz_resume_url
from app.whatsapp import WhatsAppClient
from app.workflow import build_intake_url

logger = logging.getLogger(__name__)

SKIP_LEAD_STATUSES = {
    "wrong number",
    "wrong_number",
}

COL_DATE = "daily_reminder_date"
COL_TYPE = "daily_reminder_type"


@dataclass
class DailyReminderResult:
    ok: bool = True
    quiz_sent: int = 0
    audit_sent: int = 0
    skipped: int = 0
    failed: int = 0
    dry_run: bool = False
    detail: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "quiz_sent": self.quiz_sent,
            "audit_sent": self.audit_sent,
            "skipped": self.skipped,
            "failed": self.failed,
            "dry_run": self.dry_run,
            "detail": self.detail,
            "details": (self.details or [])[:80],
        }


def _today_str(tz_name: str) -> str:
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now = datetime.now(ZoneInfo("UTC"))
    return now.strftime("%Y-%m-%d")


def _phone_key(raw: str) -> str:
    digits = _digits_only(raw or "")
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def _row_email(row: dict) -> str:
    return _normalize_email(row.get("email") or "")


class DailyReminderService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sheets = GoogleSheetsClient(settings)
        self.whatsapp = WhatsAppClient(settings)

    def enabled(self) -> bool:
        return bool(getattr(self.settings, "daily_reminders_enabled", True))

    def _booked_keys(self) -> tuple[set[str], set[str]]:
        emails: set[str] = set()
        phones: set[str] = set()
        try:
            bookings = self.sheets.list_booking_rows()
        except Exception:
            logger.exception("Failed to load Bookings for daily reminders")
            return emails, phones
        for row in bookings:
            em = _normalize_email(row.get("email") or "")
            if em:
                emails.add(em)
            pk = _phone_key(row.get("phone") or "")
            if pk:
                phones.add(pk)
        return emails, phones

    def _already_today(self, row: dict, today: str) -> bool:
        last = (row.get(COL_DATE) or "").strip()[:10]
        kind = (row.get(COL_TYPE) or "").strip().lower()
        if kind == "done":
            return True
        return last == today and kind in {"quiz", "audit"}

    def run(
        self,
        *,
        dry_run: bool = False,
        limit: int = 0,
        delay: float = 1.5,
        check_whatsapp: bool = True,
    ) -> DailyReminderResult:
        if not self.enabled():
            return DailyReminderResult(ok=True, detail="daily_reminders_disabled", dry_run=dry_run)

        tz_name = (
            (getattr(self.settings, "daily_reminder_timezone", "") or "").strip()
            or (getattr(self.settings, "content_timezone", "") or "").strip()
            or "Asia/Karachi"
        )
        today = _today_str(tz_name)
        max_n = int(limit or getattr(self.settings, "daily_reminder_max_per_run", 40) or 40)
        max_n = max(1, min(max_n, 200))

        self.sheets.ensure_lead_columns(COL_DATE, COL_TYPE)
        booked_emails, booked_phones = self._booked_keys()
        quiz_url = quiz_resume_url(self.settings)

        result = DailyReminderResult(dry_run=dry_run, detail=f"timezone={tz_name} today={today}")
        seen: set[str] = set()
        processed = 0

        for row in self.sheets.list_lead_rows():
            if processed >= max_n:
                break

            status = (row.get("status") or "").strip().lower()
            if status in SKIP_LEAD_STATUSES:
                result.skipped += 1
                continue

            diagnosis = (row.get("diagnosis") or "").strip().lower()
            email = _row_email(row)
            phone_raw = (row.get("phone") or "").strip()
            name = (row.get("name") or "").strip() or "there"
            row_num = int(row["_row"])
            phone_k = _phone_key(phone_raw)

            # Dedupe by email (preferred) or phone
            dedupe = email or phone_k
            if not dedupe:
                result.skipped += 1
                continue
            if dedupe in seen:
                continue
            seen.add(dedupe)

            booked = (email and email in booked_emails) or (
                phone_k and phone_k in booked_phones
            )
            if booked:
                if (row.get(COL_TYPE) or "").strip().lower() != "done" and not dry_run:
                    self.sheets.update_lead_fields(
                        row_num,
                        **{COL_TYPE: "done", COL_DATE: today},
                    )
                result.skipped += 1
                result.details.append(
                    {
                        "row": row_num,
                        "email": email or "-",
                        "action": "skip_booked",
                    }
                )
                continue

            if self._already_today(row, today):
                result.skipped += 1
                continue

            if diagnosis == "completed":
                kind = "audit"
                link = build_intake_url(
                    email=email,
                    intake_url="",
                    intake_base_url=self.settings.intake_base_url,
                )
            elif diagnosis in {"", "not completed"}:
                kind = "quiz"
                link = quiz_url
            else:
                result.skipped += 1
                continue

            if kind == "audit" and not link:
                result.skipped += 1
                result.details.append(
                    {
                        "row": row_num,
                        "email": email or "-",
                        "action": "skip_no_intake_url",
                    }
                )
                continue

            phone_check = validate_phone(phone_raw, self.settings.default_phone_region)
            if not phone_check.is_valid:
                result.skipped += 1
                result.details.append(
                    {
                        "row": row_num,
                        "email": email or "-",
                        "action": "skip_invalid_phone",
                        "detail": phone_check.reason,
                    }
                )
                continue

            e164 = phone_check.e164
            label: dict[str, Any] = {
                "row": row_num,
                "name": name,
                "email": email or "-",
                "phone": e164,
                "kind": kind,
            }

            if dry_run:
                processed += 1
                if kind == "quiz":
                    result.quiz_sent += 1
                else:
                    result.audit_sent += 1
                label["action"] = "dry_run_would_send"
                label["link"] = link
                result.details.append(label)
                continue

            if check_whatsapp:
                wa = self.whatsapp.check_number_on_whatsapp(e164)
                if not wa.exists:
                    result.skipped += 1
                    label["action"] = "skip_not_on_whatsapp"
                    label["detail"] = wa.detail
                    result.details.append(label)
                    processed += 1
                    if delay > 0:
                        time.sleep(delay)
                    continue

            if kind == "quiz":
                send = self.whatsapp.send_daily_quiz_reminder(
                    e164_phone=e164,
                    name=name.split()[0] if name else "there",
                    email=email,
                    quiz_url=link,
                )
            else:
                send = self.whatsapp.send_daily_audit_reminder(
                    e164_phone=e164,
                    name=name.split()[0] if name else "there",
                    email=email,
                    intake_url=link,
                )

            if send.ok:
                processed += 1
                if kind == "quiz":
                    result.quiz_sent += 1
                else:
                    result.audit_sent += 1
                label["action"] = "sent"
                label["message_id"] = send.message_id
                self.sheets.update_lead_fields(
                    row_num,
                    **{COL_TYPE: kind, COL_DATE: today},
                )
            else:
                processed += 1
                result.failed += 1
                label["action"] = "failed"
                label["detail"] = send.detail
            result.details.append(label)
            if delay > 0:
                time.sleep(delay)

        logger.info(
            "Daily reminders done quiz=%s audit=%s skipped=%s failed=%s dry_run=%s",
            result.quiz_sent,
            result.audit_sent,
            result.skipped,
            result.failed,
            dry_run,
        )
        return result

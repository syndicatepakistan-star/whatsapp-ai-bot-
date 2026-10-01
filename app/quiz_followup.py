"""Message Leads with diagnosis=Not Completed (quiz incomplete follow-up)."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote

from app.config import Settings
from app.phone_validator import validate_phone
from app.sheets import GoogleSheetsClient, _emails_from_text, _normalize_email
from app.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

SKIP_STATUSES = {
    "wrong number",
    "wrong_number",
}

ALREADY_FOLLOWED_UP = {
    "sent",
    "sent_link_then_text",
    "skipped",
    "failed",
}

# Only these lead statuses are eligible for the 10-min incomplete quiz nudge.
ELIGIBLE_LEAD_STATUSES = {
    "lead captured",
    "lead_captured",
}


def _parse_lead_timestamp(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+0000")
    formats = (
        "%Y-%m-%d %H:%M:%S UTC",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S",
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _intake_url_from_notes(notes: str) -> str:
    text = (notes or "").strip()
    if not text:
        return ""
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,;]")
        lowered = url.lower()
        if "quiz" in lowered or "intake" in lowered:
            return unquote(url)
    for match in _URL_RE.findall(text):
        return unquote(match.rstrip(").,;]"))
    return ""


def _row_email(row: dict) -> str:
    primary = _normalize_email(row.get("email") or "")
    if primary:
        return primary
    from_notes = _emails_from_text(row.get("notes") or "")
    return next(iter(sorted(from_notes)), "")


@dataclass
class FollowupRunResult:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    dry_run: bool = False
    details: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "sent": self.sent,
            "skipped": self.skipped,
            "failed": self.failed,
            "dry_run": self.dry_run,
            "details": (self.details or [])[:50],
        }


class QuizFollowupService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sheets = GoogleSheetsClient(settings)
        self.whatsapp = WhatsAppClient(settings)

    def run(
        self,
        *,
        dry_run: bool = False,
        limit: int = 0,
        delay: float = 1.5,
        force: bool = False,
        include_manual_followup: bool = False,
        check_whatsapp: bool = True,
        min_age_minutes: int | None = None,
        only_lead_captured: bool = True,
    ) -> FollowupRunResult:
        """
        Send incomplete-quiz follow-ups.

        Default: only rows with status "lead captured", diagnosis Not Completed,
        older than QUIZ_FOLLOWUP_DELAY_MINUTES (10), and no quiz_followup_status yet.
        """
        self.sheets.ensure_lead_columns("quiz_followup_status", "quiz_followup_detail")
        rows = self.sheets.list_lead_rows()
        result = FollowupRunResult(dry_run=dry_run, details=[])

        if min_age_minutes is None:
            min_age_minutes = int(self.settings.quiz_followup_delay_minutes or 10)
        min_age = max(0, int(min_age_minutes))
        now = datetime.now(timezone.utc)

        processed = 0
        for row in rows:
            if limit and processed >= limit:
                break

            diagnosis = (row.get("diagnosis") or "").strip()
            if diagnosis.lower() != "not completed":
                continue

            row_number = int(row["_row"])
            name = (row.get("name") or "").strip() or "there"
            email = _row_email(row)
            phone_raw = (row.get("phone") or "").strip()
            status = (row.get("status") or "").strip().lower()
            followup_status = (row.get("quiz_followup_status") or "").strip().lower()

            label: dict[str, Any] = {
                "row": row_number,
                "name": name,
                "email": email or "-",
                "phone": phone_raw or "-",
                "lead_status": status or "-",
            }

            if followup_status in ALREADY_FOLLOWED_UP and not force:
                result.skipped += 1
                label["action"] = "skip_already_done"
                label["detail"] = followup_status
                result.details.append(label)
                continue

            # Age gate: wait N minutes after sheet timestamp before nudging.
            if min_age > 0:
                captured_at = _parse_lead_timestamp(row.get("timestamp") or "")
                if captured_at is None:
                    result.skipped += 1
                    label["action"] = "skip_no_timestamp"
                    result.details.append(label)
                    continue
                age = now - captured_at
                if age < timedelta(minutes=min_age):
                    result.skipped += 1
                    label["action"] = "skip_too_early"
                    label["detail"] = f"age_seconds={int(age.total_seconds())}"
                    result.details.append(label)
                    continue

            if status in SKIP_STATUSES:
                result.skipped += 1
                label["action"] = "skip_wrong_number"
                if not dry_run:
                    self.sheets.update_lead_quiz_followup(
                        row_number,
                        status="skipped",
                        detail="wrong_number",
                    )
                result.details.append(label)
                processed += 1
                continue

            if phone_raw.upper() in {"#ERROR!", "#N/A", "#VALUE!", "#REF!", ""}:
                result.skipped += 1
                label["action"] = "skip_bad_phone"
                if not dry_run:
                    self.sheets.update_lead_quiz_followup(
                        row_number,
                        status="skipped",
                        detail="bad_phone",
                    )
                result.details.append(label)
                processed += 1
                continue

            if only_lead_captured and status not in ELIGIBLE_LEAD_STATUSES:
                if (
                    status in {"manual follow-up needed", "manual follow up needed"}
                    and include_manual_followup
                ):
                    pass
                else:
                    result.skipped += 1
                    label["action"] = "skip_not_lead_captured"
                    label["detail"] = status or "empty_status"
                    result.details.append(label)
                    continue

            if (
                status in {"manual follow-up needed", "manual follow up needed"}
                and not include_manual_followup
            ):
                result.skipped += 1
                label["action"] = "skip_manual_followup"
                label["detail"] = "not_on_whatsapp_previously"
                if not dry_run:
                    self.sheets.update_lead_quiz_followup(
                        row_number,
                        status="skipped",
                        detail="manual_followup_needed",
                    )
                result.details.append(label)
                processed += 1
                continue

            phone_check = validate_phone(phone_raw, self.settings.default_phone_region)
            if not phone_check.is_valid:
                result.skipped += 1
                label["action"] = "skip_invalid_phone"
                label["detail"] = phone_check.reason
                if not dry_run:
                    self.sheets.update_lead_quiz_followup(
                        row_number,
                        status="skipped",
                        detail=f"invalid_phone:{phone_check.reason}",
                    )
                result.details.append(label)
                processed += 1
                continue

            e164 = phone_check.e164
            # Follow-up always goes to quiz questions — never intake/audit form.
            configured = (self.settings.quiz_resume_url or "").strip()
            base = (self.settings.intake_base_url or "https://the-syndicate.com").rstrip("/")
            quiz_url = configured or f"{base}/quiz/questions"
            notes_url = _intake_url_from_notes(row.get("notes") or "")
            # Prefer notes URL only if it is clearly the quiz questions page.
            if notes_url and "questions" in notes_url.lower():
                quiz_url = notes_url

            if dry_run:
                result.sent += 1
                label["action"] = "dry_run_would_send"
                label["quiz_url"] = quiz_url
                label["e164"] = e164
                result.details.append(label)
                processed += 1
                continue

            if check_whatsapp:
                wa_check = self.whatsapp.check_number_on_whatsapp(e164)
                if not wa_check.exists:
                    result.skipped += 1
                    label["action"] = "skip_not_on_whatsapp"
                    label["detail"] = wa_check.detail
                    self.sheets.update_lead_quiz_followup(
                        row_number,
                        status="skipped",
                        detail=f"not_on_whatsapp:{wa_check.detail}",
                    )
                    result.details.append(label)
                    processed += 1
                    if delay > 0:
                        time.sleep(delay)
                    continue

            send = self.whatsapp.send_quiz_followup(
                e164_phone=e164,
                name=name.split()[0] if name else "there",
                email=email,
                quiz_url=quiz_url,
            )
            if send.ok:
                result.sent += 1
                label["action"] = "sent"
                label["detail"] = send.detail
                label["message_id"] = send.message_id
                self.sheets.update_lead_quiz_followup(
                    row_number,
                    status="sent",
                    detail=send.message_id or send.detail,
                )
            else:
                result.failed += 1
                label["action"] = "failed"
                label["detail"] = send.detail
                self.sheets.update_lead_quiz_followup(
                    row_number,
                    status="failed",
                    detail=(send.detail or "")[:500],
                )
            result.details.append(label)
            processed += 1
            if delay > 0:
                time.sleep(delay)

        logger.info(
            "Quiz followup done sent=%s skipped=%s failed=%s dry_run=%s min_age=%s",
            result.sent,
            result.skipped,
            result.failed,
            dry_run,
            min_age,
        )
        return result

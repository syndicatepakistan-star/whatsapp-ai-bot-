"""Message Leads with diagnosis=Not Completed (quiz incomplete follow-up)."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from app.config import Settings
from app.phone_validator import validate_phone
from app.sheets import GoogleSheetsClient, _emails_from_text, _normalize_email
from app.whatsapp import WhatsAppClient
from app.workflow import build_intake_url

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


def _intake_url_from_notes(notes: str) -> str:
    text = (notes or "").strip()
    if not text:
        return ""
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,;]")
        lowered = url.lower()
        if "quiz" in lowered or "intake" in lowered:
            return unquote(url)
    # Fallback: first URL in notes
    for match in _URL_RE.findall(text):
        return unquote(match.rstrip(").,;]"))
    return ""


def _row_email(row: dict) -> str:
    primary = _normalize_email(row.get("email") or "")
    if primary:
        return primary
    from_notes = _emails_from_text(row.get("notes") or "")
    return next(iter(sorted(from_notes)), "")


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
    ) -> FollowupRunResult:
        self.sheets.ensure_lead_columns("quiz_followup_status", "quiz_followup_detail")
        rows = self.sheets.list_lead_rows()
        result = FollowupRunResult(dry_run=dry_run, details=[])

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

            label = {
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
            intake_from_notes = _intake_url_from_notes(row.get("notes") or "")
            intake_url = build_intake_url(
                email=email,
                intake_url=intake_from_notes,
                intake_base_url=self.settings.intake_base_url,
            )
            if not intake_url:
                result.skipped += 1
                label["action"] = "skip_no_intake_url"
                if not dry_run:
                    self.sheets.update_lead_quiz_followup(
                        row_number,
                        status="skipped",
                        detail="no_intake_url",
                    )
                result.details.append(label)
                processed += 1
                continue

            if dry_run:
                result.sent += 1  # would-send count under dry_run
                label["action"] = "dry_run_would_send"
                label["intake_url"] = intake_url
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
                intake_url=intake_url,
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
            "Quiz followup done sent=%s skipped=%s failed=%s dry_run=%s",
            result.sent,
            result.skipped,
            result.failed,
            dry_run,
        )
        return result

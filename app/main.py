from __future__ import annotations

import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.booking_workflow import BookingWorkflow
from app.config import get_settings
from app.sheets import GoogleSheetsClient
from app.whatsapp import WhatsAppClient
from app.workflow import LeadWorkflow
from sub_agent_b.folder_scan import FolderScanner
from sub_agent_b.media_whapi import MediaWhapiClient
from sub_agent_b.poster import ContentPoster

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Syndicate Lead WhatsApp Bot",
    description="Webhook receiver: quiz leads + audit booking + Sub-agent B content poster (Whapi)",
    version="1.3.0",
)


class LeadPayload(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=40)
    source: str = Field(default="", max_length=100)
    intake_url: str = Field(default="", max_length=500)
    diagnosis: str = Field(default="Not Completed", max_length=32)
    # True = only update diagnosis on Leads sheet (no WhatsApp / group add).
    sheet_only: bool = False


class DiagnosisBackfillItem(BaseModel):
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=40)
    diagnosis: str = Field(default="Not Completed", max_length=32)


class DiagnosisBackfillPayload(BaseModel):
    items: list[DiagnosisBackfillItem] = Field(default_factory=list)


class BookingPayload(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=40)
    meet_link: str = Field(default="", max_length=500)
    slot_start: str = Field(default="", max_length=64)
    slot_end: str = Field(default="", max_length=64)
    timezone: str = Field(default="Asia/Karachi", max_length=64)
    source: str = Field(default="", max_length=100)
    intake_ref: str = Field(default="", max_length=128)
    booking_id: int | str | None = None


def _check_webhook_secret(x_webhook_secret: str | None) -> None:
    settings = get_settings()
    if settings.webhook_secret:
        if (x_webhook_secret or "") != settings.webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/admin/groups")
def list_groups(
    x_webhook_secret: str | None = Header(default=None),
    count: int = 100,
) -> dict[str, Any]:
    """
    List WhatsApp groups for the linked Whapi number.
    Use this to copy WHAPI_GROUP_ID (id ends with @g.us).
    Auth: same WEBHOOK_SECRET as other webhooks (X-Webhook-Secret).
    """
    _check_webhook_secret(x_webhook_secret)
    settings = get_settings()
    client = WhatsAppClient(settings)
    result = client.list_groups(count=count, offset=0)
    result["configured_group_id"] = (settings.whapi_group_id or "").strip()
    result["group_add_enabled"] = bool(settings.whapi_group_add_enabled)
    return result


class QuizFollowupPayload(BaseModel):
    dry_run: bool = False
    limit: int = Field(default=0, ge=0, le=500)
    delay: float = Field(default=1.5, ge=0, le=30)
    force: bool = False
    include_manual_followup: bool = False
    skip_whatsapp_check: bool = False


@app.post("/admin/quiz-followup")
def quiz_followup(
    payload: QuizFollowupPayload = QuizFollowupPayload(),
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    WhatsApp follow-up for Leads where diagnosis = Not Completed.
    Same link-then-text pattern as audit booking. Auth: X-Webhook-Secret.
    Prefer dry_run first. Or use: python scripts/message_not_completed.py --dry-run
    """
    _check_webhook_secret(x_webhook_secret)
    from app.quiz_followup import QuizFollowupService

    settings = get_settings()
    result = QuizFollowupService(settings).run(
        dry_run=bool(payload.dry_run),
        limit=int(payload.limit or 0),
        delay=float(payload.delay),
        force=bool(payload.force),
        include_manual_followup=bool(payload.include_manual_followup),
        check_whatsapp=not bool(payload.skip_whatsapp_check),
    )
    return result.as_dict()


@app.post("/webhook/lead")
def receive_lead(
    payload: LeadPayload,
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    Website calls this when a quiz lead is saved.
    Purpose of this webhook URL: bridge between website and this automation.
    """
    _check_webhook_secret(x_webhook_secret)
    settings = get_settings()

    if not (payload.phone or "").strip() and not bool(payload.sheet_only):
        raise HTTPException(status_code=400, detail="phone is required")
    if (
        bool(payload.sheet_only)
        and not (payload.phone or "").strip()
        and not (payload.email or "").strip()
    ):
        raise HTTPException(
            status_code=400,
            detail="phone or email is required when sheet_only=true",
        )

    logger.info(
        "Lead received name=%s email=%s phone=%s source=%s diagnosis=%s sheet_only=%s intake_url=%s",
        payload.name,
        payload.email,
        payload.phone,
        payload.source or "unknown",
        payload.diagnosis or "Not Completed",
        payload.sheet_only,
        (payload.intake_url or "")[:120],
    )

    workflow = LeadWorkflow(settings)
    result = workflow.process(
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        intake_url=payload.intake_url,
        diagnosis=payload.diagnosis or "Not Completed",
        sheet_only=bool(payload.sheet_only),
    )

    return {
        "ok": result.action != "sheet_diagnosis_skipped",
        "sheet_updated": result.action == "sheet_diagnosis_updated",
        "action": result.action,
        "status": result.status,
        "phone_e164": result.phone_e164,
        "intake_url": result.intake_url,
        "detail": result.detail,
        "group_add_status": result.group_add_status,
        "group_add_detail": result.group_add_detail,
        "diagnosis": result.diagnosis,
    }


@app.post("/webhook/leads/backfill-diagnosis")
def backfill_diagnosis(
    payload: DiagnosisBackfillPayload,
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    Batch-update diagnosis on existing Leads rows (one sheet read + batched writes).
    Used by Django manage.py backfill_lead_diagnosis --batch.
    """
    _check_webhook_secret(x_webhook_secret)
    settings = get_settings()
    items = [
        {
            "email": item.email,
            "phone": item.phone,
            "diagnosis": item.diagnosis,
        }
        for item in (payload.items or [])
    ]
    if not items:
        raise HTTPException(status_code=400, detail="items is required")

    logger.info("Diagnosis batch backfill items=%s", len(items))
    sheets = GoogleSheetsClient(settings)
    result = sheets.batch_update_diagnoses(items)
    return {"ok": True, **result}


@app.post("/webhook/booking")
def receive_booking(
    payload: BookingPayload,
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    Website calls this after a successful audit booking.
    Sends WhatsApp with Meet link and logs to Bookings sheet.
    """
    _check_webhook_secret(x_webhook_secret)
    settings = get_settings()

    if not (payload.phone or "").strip():
        raise HTTPException(status_code=400, detail="phone is required")
    if not (payload.meet_link or "").strip():
        raise HTTPException(status_code=400, detail="meet_link is required")
    if not (payload.slot_start or "").strip():
        raise HTTPException(status_code=400, detail="slot_start is required")

    booking_id = "" if payload.booking_id is None else str(payload.booking_id)

    logger.info(
        "Booking received name=%s email=%s phone=%s slot=%s meet=%s booking_id=%s",
        payload.name,
        payload.email,
        payload.phone,
        payload.slot_start,
        (payload.meet_link or "")[:80],
        booking_id,
    )

    workflow = BookingWorkflow(settings)
    result = workflow.process(
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        meet_link=payload.meet_link,
        slot_start=payload.slot_start,
        slot_end=payload.slot_end,
        timezone_name=payload.timezone or "Asia/Karachi",
        booking_id=booking_id,
    )

    return {
        "ok": True,
        "action": result.action,
        "status": result.status,
        "phone_e164": result.phone_e164,
        "slot_local": result.slot_local,
        "detail": result.detail,
    }


@app.post("/cron/reminders")
@app.get("/cron/reminders")
def run_reminders(
    x_cron_secret: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    Call every 5–15 minutes (Railway cron / external ping).
    Sends WhatsApp reminders for upcoming booked audits.
    Auth: CRON_SECRET (X-Cron-Secret) or WEBHOOK_SECRET (X-Webhook-Secret).
    """
    settings = get_settings()
    expected = (settings.cron_secret or settings.webhook_secret or "").strip()
    provided = (x_cron_secret or x_webhook_secret or "").strip()
    if expected and provided != expected:
        raise HTTPException(status_code=401, detail="Invalid cron/webhook secret")

    workflow = BookingWorkflow(settings)
    result = workflow.process_due_reminders()
    logger.info("Reminders tick result=%s", result)
    return result


def _check_cron_secret(
    x_cron_secret: str | None,
    x_webhook_secret: str | None,
) -> None:
    settings = get_settings()
    expected = (settings.cron_secret or settings.webhook_secret or "").strip()
    provided = (x_cron_secret or x_webhook_secret or "").strip()
    if expected and provided != expected:
        raise HTTPException(status_code=401, detail="Invalid cron/webhook secret")


@app.get("/admin/channels")
def list_channels(
    x_webhook_secret: str | None = Header(default=None),
    count: int = 100,
) -> dict[str, Any]:
    """
    List WhatsApp channels (newsletters) for the linked Whapi number.
    Use this to copy WHAPI_CHANNEL_ID (id ends with @newsletter).
    Auth: X-Webhook-Secret when WEBHOOK_SECRET is set.
    """
    _check_webhook_secret(x_webhook_secret)
    settings = get_settings()
    result = MediaWhapiClient(settings).list_newsletters(count=count, offset=0)
    result["configured_channel_id"] = (settings.whapi_channel_id or "").strip()
    result["configured_group_id"] = (settings.whapi_group_id or "").strip()
    result["content_poster_enabled"] = bool(settings.content_poster_enabled)
    return result


class ContentScanPayload(BaseModel):
    """Optional body for POST /admin/content-scan-folder."""

    folder: str = Field(
        default="",
        max_length=500,
        description="Drive folder URL or ID (defaults to GOOGLE_DRIVE_CONTENT_FOLDER_ID)",
    )
    count: int | None = Field(default=None, ge=1, le=365)
    time: str = Field(default="", max_length=16, description="HH:MM post time")
    target: str = Field(default="", max_length=16, description="group | channel | both")
    caption: str = Field(default="", max_length=2000)
    start_date: str = Field(
        default="",
        max_length=16,
        description="YYYY-MM-DD; default = day after last pending, or today",
    )
    caption_from_filename: bool | None = None


@app.post("/admin/content-scan-folder")
def scan_content_folder(
    payload: ContentScanPayload = ContentScanPayload(),
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    Scan the shared Drive content folder and append pending ContentCalendar rows.

    - One unused file → one day (sequential dates)
    - Auto-detects type (image / video / audio / voice / document)
    - Skips files already listed in the sheet
    Auth: X-Webhook-Secret when WEBHOOK_SECRET is set.
    """
    _check_webhook_secret(x_webhook_secret)
    settings = get_settings()
    body = payload

    result = FolderScanner(settings).scan_and_fill(
        folder=(body.folder or "").strip() or None,
        count=body.count,
        post_time=(body.time or "").strip() or None,
        target=(body.target or "").strip() or None,
        caption=body.caption if body.caption else None,
        start_date=(body.start_date or "").strip() or None,
        caption_from_filename=body.caption_from_filename,
    )
    logger.info(
        "Content folder scan ok=%s folder=%s created=%s detail=%s",
        result.ok,
        result.folder_id,
        result.created,
        result.detail,
    )
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.detail)
    return {
        "ok": True,
        "folder_id": result.folder_id,
        "scanned": result.scanned,
        "skipped_used": result.skipped_used,
        "skipped_unsupported": result.skipped_unsupported,
        "created": result.created,
        "detail": result.detail,
        "rows": [
            {
                "row": r.get("_row"),
                "date": r.get("date"),
                "time": r.get("time"),
                "type": r.get("type"),
                "file_url": r.get("file_url"),
                "caption": r.get("caption"),
                "status": r.get("status"),
            }
            for r in result.rows
        ],
    }


@app.post("/cron/content-posts")
@app.get("/cron/content-posts")
def run_content_posts(
    background_tasks: BackgroundTasks,
    x_cron_secret: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
    wait: bool = False,
) -> dict[str, Any]:
    """
    Sub-agent B cron: post due ContentCalendar rows to group and/or channel.

    By default returns immediately and processes in the background so external
    cron (cron-job.org ~15–30s) does not get 502 while video encode runs.
    Pass ?wait=1 to run synchronously (local debugging).
    """
    _check_cron_secret(x_cron_secret, x_webhook_secret)
    settings = get_settings()

    if wait:
        result = ContentPoster(settings).run_due()
        logger.info(
            "Content posts tick processed=%s posted=%s failed=%s detail=%s",
            result.processed,
            result.posted,
            result.failed,
            result.detail,
        )
        return {
            "ok": result.ok,
            "processed": result.processed,
            "posted": result.posted,
            "failed": result.failed,
            "skipped": result.skipped,
            "detail": result.detail,
            "details": result.details,
        }

    def _tick() -> None:
        try:
            result = ContentPoster(settings).run_due()
            logger.info(
                "Content posts background tick processed=%s posted=%s failed=%s detail=%s",
                result.processed,
                result.posted,
                result.failed,
                result.detail,
            )
        except Exception:
            logger.exception("Content posts background tick failed")

    background_tasks.add_task(_tick)
    return {
        "ok": True,
        "accepted": True,
        "detail": "content_posts_started_in_background",
    }

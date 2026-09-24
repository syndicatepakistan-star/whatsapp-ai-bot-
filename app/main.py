from __future__ import annotations

import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.booking_workflow import BookingWorkflow
from app.config import get_settings
from app.whatsapp import WhatsAppClient
from app.workflow import LeadWorkflow
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
    version="1.2.0",
)


class LeadPayload(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=40)
    source: str = Field(default="", max_length=100)
    intake_url: str = Field(default="", max_length=500)


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

    if not (payload.phone or "").strip():
        raise HTTPException(status_code=400, detail="phone is required")

    logger.info(
        "Lead received name=%s email=%s phone=%s source=%s intake_url=%s",
        payload.name,
        payload.email,
        payload.phone,
        payload.source or "unknown",
        (payload.intake_url or "")[:120],
    )

    workflow = LeadWorkflow(settings)
    result = workflow.process(
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        intake_url=payload.intake_url,
    )

    return {
        "ok": True,
        "action": result.action,
        "status": result.status,
        "phone_e164": result.phone_e164,
        "intake_url": result.intake_url,
        "detail": result.detail,
        "group_add_status": result.group_add_status,
        "group_add_detail": result.group_add_detail,
    }


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

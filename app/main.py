from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.workflow import LeadWorkflow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Syndicate Lead WhatsApp Bot",
    description="Webhook receiver: validate phone → Google Sheet / WhatsApp template",
    version="1.0.0",
)


class LeadPayload(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=40)
    source: str = Field(default="", max_length=100)
    intake_url: str = Field(default="", max_length=500)


@app.get("/health")
def health() -> dict[str, object]:
    settings = get_settings()
    has_whapi = bool((settings.whapi_token or "").strip())
    has_sheet_id = bool((settings.google_sheet_id or "").strip())
    has_sheet_creds = bool(
        (settings.google_service_account_json or "").strip()
        or (settings.google_service_account_file or "").strip()
    )
    return {
        "status": "ok",
        "whapi_configured": has_whapi,
        "google_sheet_id_set": has_sheet_id,
        "google_creds_set": has_sheet_creds,
        "provider": "whapi",
    }


@app.on_event("startup")
def log_config_status() -> None:
    settings = get_settings()
    logger.info(
        "Startup config: whapi=%s sheet_id=%s sheet_creds=%s",
        bool((settings.whapi_token or "").strip()),
        bool((settings.google_sheet_id or "").strip()),
        bool((settings.google_service_account_json or "").strip()),
    )


@app.post("/webhook/lead")
def receive_lead(
    payload: LeadPayload,
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    Website calls this when a quiz lead is saved.
    Purpose of this webhook URL: bridge between website and this automation.
    """
    settings = get_settings()

    if settings.webhook_secret:
        if (x_webhook_secret or "") != settings.webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

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
    }

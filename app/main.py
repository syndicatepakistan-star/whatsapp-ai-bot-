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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
        "Lead received name=%s email=%s phone=%s source=%s",
        payload.name,
        payload.email,
        payload.phone,
        payload.source or "unknown",
    )

    workflow = LeadWorkflow(settings)
    result = workflow.process(
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
    )

    return {
        "ok": True,
        "action": result.action,
        "status": result.status,
        "phone_e164": result.phone_e164,
        "detail": result.detail,
    }

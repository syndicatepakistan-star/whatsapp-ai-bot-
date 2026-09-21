"""Sub-agent A: add WhatsApp leads to a private group (direct add + invite fallback)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import Settings
from app.whatsapp import GroupAddResult, WhatsAppClient, WhatsAppSendResult

logger = logging.getLogger(__name__)


@dataclass
class CommunityAddResult:
    action: str
    status: str
    detail: str = ""


class CommunityAgent:
    """
    After a lead receives the WhatsApp message successfully:
    1) Try direct add to WHAPI_GROUP_ID
    2) If blocked, optionally DM group/channel invite links
    """

    def __init__(self, settings: Settings, whatsapp: WhatsAppClient | None = None) -> None:
        self.settings = settings
        self.whatsapp = whatsapp or WhatsAppClient(settings)

    def enabled(self) -> bool:
        return bool(self.settings.whapi_group_add_enabled) and bool(
            (self.settings.whapi_group_id or "").strip()
        )

    def add_lead_to_group(self, *, name: str, e164_phone: str) -> CommunityAddResult:
        if not self.settings.whapi_group_add_enabled:
            return CommunityAddResult(
                action="group_add_skipped",
                status="skipped",
                detail="whapi_group_add_disabled",
            )

        group_id = (self.settings.whapi_group_id or "").strip()
        if not group_id:
            return CommunityAddResult(
                action="group_add_skipped",
                status="skipped",
                detail="whapi_group_id_missing",
            )

        add: GroupAddResult = self.whatsapp.add_to_group(
            e164_phone=e164_phone,
            group_id=group_id,
        )
        if add.ok:
            logger.info("Group add OK phone=%s group=%s", e164_phone, group_id)
            return CommunityAddResult(
                action="group_added",
                status="added",
                detail=add.detail or "added",
            )

        logger.warning(
            "Group direct add failed phone=%s detail=%s — trying invite fallback",
            e164_phone,
            add.detail,
        )
        fallback = self._send_invite_fallback(name=name, e164_phone=e164_phone)
        if fallback.ok:
            return CommunityAddResult(
                action="group_invite_sent",
                status="invite_sent",
                detail=f"direct_add_failed:{add.detail}; fallback_sent",
            )

        return CommunityAddResult(
            action="group_add_failed",
            status="failed",
            detail=f"direct_add:{add.detail}; fallback:{fallback.detail}",
        )

    def _send_invite_fallback(
        self,
        *,
        name: str,
        e164_phone: str,
    ) -> WhatsAppSendResult:
        group_link = (self.settings.wa_group_invite_url or "").strip()
        channel_link = (self.settings.wa_channel_invite_url or "").strip()
        if not group_link and not channel_link:
            return WhatsAppSendResult(False, detail="no_invite_urls_configured")

        template = self.settings.whapi_group_invite_fallback_text or (
            "Hi {name},\n\nJoin here:\n{group_link}\n\n{channel_link}"
        )
        body = (
            template.replace("\\n", "\n")
            .replace("{name}", (name or "there").strip() or "there")
            .replace("{group_link}", group_link or "(group link soon)")
            .replace("{channel_link}", channel_link or "(channel link soon)")
        ).strip()
        # Drop empty placeholder lines a bit
        while "\n\n\n" in body:
            body = body.replace("\n\n\n", "\n\n")

        return self.whatsapp.send_raw_text(e164_phone=e164_phone, body=body)

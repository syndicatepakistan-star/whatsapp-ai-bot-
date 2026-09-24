"""
Whapi media helpers for Sub-agent B.

Sends to a chat id (group @g.us or channel @newsletter) via:
  POST /messages/text|image|video|voice|audio|document

Supports:
- public HTTPS media URL (JSON body)
- local file upload (multipart) — required for Google Drive after local download
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

SUPPORTED_TYPES = frozenset({"text", "image", "video", "voice", "audio", "document"})


@dataclass
class MediaSendResult:
    ok: bool
    detail: str = ""
    message_id: str = ""


class MediaWhapiClient:
    """Thin Whapi client focused on group/channel publishing."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _configured(self) -> bool:
        return bool((self.settings.whapi_token or "").strip())

    def _base_url(self) -> str:
        return (self.settings.whapi_api_url or "https://gate.whapi.cloud").rstrip("/")

    def _headers_json(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.whapi_token.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _headers_auth(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.whapi_token.strip()}",
            "Accept": "application/json",
        }

    def send_to_chat(
        self,
        *,
        chat_id: str,
        media_type: str,
        caption: str = "",
        file_url: str = "",
        file_path: str | Path | None = None,
        filename: str = "",
    ) -> MediaSendResult:
        """
        Send one item to group or channel.

        Prefer file_path (multipart upload) when media was downloaded locally
        (e.g. Google Drive). Otherwise pass a direct public file_url.
        """
        if not self._configured():
            return MediaSendResult(False, detail="whapi_token_missing")

        to = (chat_id or "").strip()
        if not to:
            return MediaSendResult(False, detail="empty_chat_id")

        kind = (media_type or "text").strip().lower()
        if kind not in SUPPORTED_TYPES:
            return MediaSendResult(False, detail=f"unsupported_type:{kind}")

        body_text = (caption or "").replace("\\n", "\n").strip()
        media = (file_url or "").strip()
        local = Path(file_path) if file_path else None

        if kind == "text":
            if not body_text:
                return MediaSendResult(False, detail="empty_caption_for_text")
            return self._post_json(
                path="/messages/text",
                payload={"to": to, "body": body_text},
            )

        if local is not None and local.is_file():
            return self._post_multipart(
                kind=kind,
                to=to,
                caption=body_text,
                file_path=local,
                filename=filename or local.name,
            )

        if not media:
            return MediaSendResult(False, detail=f"file_url_required_for_{kind}")

        payload: dict[str, Any] = {"to": to, "media": media}
        if body_text and kind in {"image", "video", "document"}:
            payload["caption"] = body_text
        if kind == "document":
            name = media.rstrip("/").split("/")[-1].split("?")[0]
            if name:
                payload["filename"] = name

        return self._post_json(path=f"/messages/{kind}", payload=payload)

    def list_newsletters(self, *, count: int = 100, offset: int = 0) -> dict[str, Any]:
        """List WhatsApp channels (newsletters) for WHAPI_CHANNEL_ID discovery."""
        if not self._configured():
            return {"ok": False, "detail": "whapi_token_missing", "channels": []}

        url = f"{self._base_url()}/newsletters"
        params = {"count": count, "offset": offset}
        try:
            with httpx.Client(timeout=45.0) as client:
                response = client.get(url, headers=self._headers_json(), params=params)
        except httpx.HTTPError as exc:
            logger.exception("Whapi list newsletters failed")
            return {"ok": False, "detail": str(exc), "channels": []}

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}

        if response.status_code >= 400:
            return {
                "ok": False,
                "detail": str(data),
                "channels": [],
                "status_code": response.status_code,
            }

        raw_list = []
        if isinstance(data, dict):
            raw_list = data.get("newsletters") or data.get("channels") or data.get("data") or []
        elif isinstance(data, list):
            raw_list = data

        simplified = []
        for item in raw_list or []:
            if not isinstance(item, dict):
                continue
            simplified.append(
                {
                    "id": item.get("id") or item.get("chat_id") or "",
                    "name": item.get("name") or item.get("title") or "",
                    "raw": item,
                }
            )

        return {
            "ok": True,
            "channels": simplified,
            "count": len(simplified),
            "total": (data.get("total") if isinstance(data, dict) else len(simplified)),
            "offset": offset,
        }

    def _post_json(self, *, path: str, payload: dict[str, Any]) -> MediaSendResult:
        url = f"{self._base_url()}{path}"
        try:
            with httpx.Client(timeout=120.0) as client:
                response = client.post(url, headers=self._headers_json(), json=payload)
        except httpx.HTTPError as exc:
            logger.exception("Whapi media send failed path=%s", path)
            return MediaSendResult(False, detail=f"request_error: {exc}")
        return self._parse_send_response(response)

    def _post_multipart(
        self,
        *,
        kind: str,
        to: str,
        caption: str,
        file_path: Path,
        filename: str,
    ) -> MediaSendResult:
        url = f"{self._base_url()}/messages/{kind}"
        data: dict[str, str] = {"to": to}
        if caption and kind in {"image", "video", "document"}:
            data["caption"] = caption
        if kind == "document" and filename:
            data["filename"] = filename
        # Do not send no_encode in multipart: Whapi schema requires boolean,
        # and form fields are always strings → 400 "/body/no_encode must be boolean".

        try:
            with httpx.Client(timeout=300.0) as client:
                with file_path.open("rb") as handle:
                    files = {"media": (filename or file_path.name, handle)}
                    response = client.post(
                        url,
                        headers=self._headers_auth(),
                        data=data,
                        files=files,
                    )
        except httpx.HTTPError as exc:
            logger.exception("Whapi multipart send failed kind=%s", kind)
            return MediaSendResult(False, detail=f"request_error: {exc}")
        return self._parse_send_response(response)

    def _parse_send_response(self, response: httpx.Response) -> MediaSendResult:
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}

        if response.status_code >= 400:
            logger.warning("Whapi media error %s data=%s", response.status_code, data)
            return MediaSendResult(False, detail=str(data))

        message_id = ""
        if isinstance(data, dict):
            message_id = str(
                data.get("message", {}).get("id")
                or data.get("id")
                or data.get("message_id")
                or ""
            )
        return MediaSendResult(True, detail="sent", message_id=message_id)

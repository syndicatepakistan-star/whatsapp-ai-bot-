"""
Sub-agent B orchestration.

Reads ContentCalendar sheet → posts due items to WhatsApp group and/or channel.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings
from sub_agent_b.content_sheets import ContentSheetsClient, parse_row_datetime
from sub_agent_b.media_whapi import MediaWhapiClient

logger = logging.getLogger(__name__)


@dataclass
class ContentRunResult:
    ok: bool
    processed: int = 0
    posted: int = 0
    failed: int = 0
    skipped: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""


class ContentPoster:
    """Sheet-driven publisher for group + channel."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sheets = ContentSheetsClient(settings)
        self.media = MediaWhapiClient(settings)

    def enabled(self) -> bool:
        return bool(getattr(self.settings, "content_poster_enabled", True))

    def run_due(self) -> ContentRunResult:
        if not self.enabled():
            return ContentRunResult(ok=True, detail="content_poster_disabled")

        group_id = (self.settings.whapi_group_id or "").strip()
        channel_id = (getattr(self.settings, "whapi_channel_id", "") or "").strip()
        if not group_id and not channel_id:
            return ContentRunResult(
                ok=False,
                detail="missing_whapi_group_id_and_channel_id",
            )

        tz_name = (getattr(self.settings, "content_timezone", "") or "Asia/Karachi").strip()
        try:
            now = datetime.now(ZoneInfo(tz_name))
        except Exception:
            now = datetime.now(ZoneInfo("UTC"))
            tz_name = "UTC"

        max_per_run = int(getattr(self.settings, "content_poster_max_per_run", 5) or 5)
        delay = float(getattr(self.settings, "content_poster_delay_seconds", 2) or 0)

        try:
            pending = self.sheets.list_pending_rows()
        except Exception as exc:
            logger.exception("ContentCalendar load failed")
            return ContentRunResult(ok=False, detail=f"sheet_error: {exc}")

        due_rows = []
        for row in pending:
            when = parse_row_datetime(row, tz_name)
            if when is None:
                due_rows.append((row, "bad_datetime"))
                continue
            if when <= now:
                due_rows.append((row, "due"))

        result = ContentRunResult(ok=True, detail=f"timezone={tz_name}")
        for row, reason in due_rows:
            if result.processed >= max_per_run:
                break
            if reason == "bad_datetime":
                row_num = int(row["_row"])
                self.sheets.mark_result(
                    row_num,
                    status="failed",
                    notes="Could not parse date/time. Use YYYY-MM-DD and HH:MM",
                )
                result.failed += 1
                result.processed += 1
                result.details.append({"row": row_num, "status": "failed", "detail": "bad_datetime"})
                continue

            item = self._post_row(row, group_id=group_id, channel_id=channel_id)
            result.processed += 1
            if item.get("status") == "posted":
                result.posted += 1
            elif item.get("status") == "failed":
                result.failed += 1
            else:
                result.skipped += 1
            result.details.append(item)
            if delay > 0:
                time.sleep(delay)

        logger.info(
            "ContentPoster done processed=%s posted=%s failed=%s",
            result.processed,
            result.posted,
            result.failed,
        )
        return result

    def _post_row(
        self,
        row: dict,
        *,
        group_id: str,
        channel_id: str,
    ) -> dict[str, Any]:
        row_num = int(row["_row"])
        media_type = (row.get("type") or "text").strip().lower() or "text"
        caption = (row.get("caption") or "").strip()
        file_url = (row.get("file_url") or "").strip()
        target = (row.get("target") or "both").strip().lower() or "both"

        destinations: list[tuple[str, str]] = []
        if target in {"group", "both"} and group_id:
            destinations.append(("group", group_id))
        if target in {"channel", "both"} and channel_id:
            destinations.append(("channel", channel_id))

        if not destinations:
            note = f"no_destination_for_target={target}"
            self.sheets.mark_result(row_num, status="failed", notes=note)
            return {"row": row_num, "status": "failed", "detail": note}

        resolved_path = None
        resolved_name = ""
        resolve_source = ""
        cleanup_paths: list = []
        try:
            if media_type != "text":
                if not file_url and media_type != "text":
                    note = "file_url_required_for_media"
                    self.sheets.mark_result(row_num, status="failed", notes=note)
                    return {"row": row_num, "status": "failed", "detail": note}
                from sub_agent_b.media_resolve import MediaResolver

                # Always download URL → local playable file, then multipart upload.
                # Never pass raw TikTok/page URLs to Whapi (those don't play).
                resolved = MediaResolver(self.settings).resolve(
                    file_url, media_type=media_type
                )
                resolved_path = resolved.path
                resolved_name = resolved.filename
                resolve_source = resolved.source
                if resolved.cleanup_paths:
                    cleanup_paths.extend(resolved.cleanup_paths)
                logger.info(
                    "Resolved media row=%s source=%s file=%s bytes=%s",
                    row_num,
                    resolve_source,
                    resolved_name,
                    resolved_path.stat().st_size if resolved_path else 0,
                )
        except Exception as exc:
            logger.exception("Media resolve failed row=%s", row_num)
            note = f"media_resolve_failed: {exc}"
            self.sheets.mark_result(row_num, status="failed", notes=note)
            return {"row": row_num, "status": "failed", "detail": note}

        notes_parts: list[str] = []
        all_ok = True
        try:
            for label, chat_id in destinations:
                if media_type == "text":
                    send = self.media.send_to_chat(
                        chat_id=chat_id,
                        media_type="text",
                        caption=caption,
                    )
                else:
                    send = self.media.send_to_chat(
                        chat_id=chat_id,
                        media_type=media_type,
                        caption=caption,
                        file_url="",
                        file_path=resolved_path,
                        filename=resolved_name,
                    )
                if send.ok:
                    notes_parts.append(f"{label}:ok:{send.message_id or 'sent'}")
                else:
                    all_ok = False
                    notes_parts.append(f"{label}:fail:{send.detail}")
        finally:
            seen: set[str] = set()
            paths_to_delete = list(cleanup_paths)
            if resolved_path is not None:
                paths_to_delete.append(resolved_path)
            for p in paths_to_delete:
                key = str(p)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    if p.is_dir():
                        import shutil

                        shutil.rmtree(p, ignore_errors=True)
                    elif p.is_file():
                        p.unlink(missing_ok=True)
                except Exception:
                    logger.warning("Could not cleanup temp media %s", p)

        if resolve_source:
            notes_parts.append(f"resolve:{resolve_source}")

        status = "posted" if all_ok else "failed"
        notes = "; ".join(notes_parts)
        self.sheets.mark_result(row_num, status=status, notes=notes)
        return {"row": row_num, "status": status, "detail": notes, "type": media_type, "target": target}

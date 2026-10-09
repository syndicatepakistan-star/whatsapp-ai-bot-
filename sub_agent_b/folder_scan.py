"""
Scan a Google Drive folder and append pending ContentCalendar rows.

One file → one day (sequential dates). Type is auto-detected from MIME / extension.
Skips files already present in the sheet (any status).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from sub_agent_b.content_sheets import ContentSheetsClient, parse_row_datetime
from sub_agent_b.media_resolve import MediaResolver, extract_drive_file_id, extract_drive_folder_id

logger = logging.getLogger(__name__)

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_AUDIO_EXT = {".mp3", ".m4a", ".wav", ".aac", ".flac"}
_VOICE_EXT = {".ogg", ".opus"}
_DOC_EXT = {".pdf", ".doc", ".docx", ".txt", ".rtf"}


@dataclass
class DriveFolderFile:
    id: str
    name: str
    mime_type: str


@dataclass
class FolderScanResult:
    ok: bool
    folder_id: str = ""
    scanned: int = 0
    skipped_used: int = 0
    skipped_unsupported: int = 0
    created: int = 0
    detail: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)


def detect_media_type(name: str, mime_type: str = "") -> str | None:
    """Map Drive file name + MIME to ContentCalendar type, or None if unsupported."""
    mime = (mime_type or "").strip().lower()
    if mime.startswith("application/vnd.google-apps."):
        return None
    if mime == "application/vnd.google-apps.folder":
        return None

    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime in ("audio/ogg", "application/ogg") or mime.endswith("opus"):
        return "voice"
    if mime.startswith("audio/"):
        return "audio"
    if mime in ("text/plain",):
        return "document"
    if mime in ("application/pdf",) or mime.startswith("application/msword"):
        return "document"
    if mime.startswith("application/vnd.openxmlformats"):
        return "document"

    ext = Path(name or "").suffix.lower()
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _VIDEO_EXT:
        return "video"
    if ext in _VOICE_EXT:
        return "voice"
    if ext in _AUDIO_EXT:
        return "audio"
    if ext in _DOC_EXT:
        return "document"
    return None


def weekend_dates_from(start: date, count: int) -> list[date]:
    """Return the next `count` Saturday/Sunday dates starting at/after `start`."""
    if count <= 0:
        return []
    day = start
    # Move to Saturday (5) or Sunday (6); if weekday, jump to this week's Saturday
    while day.weekday() not in (5, 6):
        day += timedelta(days=1)
    out: list[date] = []
    while len(out) < count:
        if day.weekday() in (5, 6):
            out.append(day)
        day += timedelta(days=1)
    return out


def drive_file_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def _natural_sort_key(name: str) -> tuple:
    """Sort 2 before 10 (natural order on digit groups)."""
    parts = re.split(r"(\d+)", (name or "").lower())
    key: list = []
    for part in parts:
        if part.isdigit():
            key.append(int(part))
        else:
            key.append(part)
    return tuple(key)


class FolderScanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sheets = ContentSheetsClient(settings)
        self.resolver = MediaResolver(settings)

    def list_folder_files(self, folder_id: str) -> list[DriveFolderFile]:
        token = self.resolver._drive_access_token()
        files: list[DriveFolderFile] = []
        page_token: str | None = None
        query = (
            f"'{folder_id}' in parents and trashed = false "
            f"and mimeType != 'application/vnd.google-apps.folder'"
        )
        with httpx.Client(timeout=60.0) as client:
            while True:
                params: dict[str, Any] = {
                    "q": query,
                    "fields": "nextPageToken,files(id,name,mimeType)",
                    "pageSize": 1000,
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                }
                if page_token:
                    params["pageToken"] = page_token
                response = client.get(
                    "https://www.googleapis.com/drive/v3/files",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"drive_list_error_{response.status_code}: {response.text[:300]}"
                    )
                data = response.json()
                for item in data.get("files") or []:
                    fid = str(item.get("id") or "").strip()
                    if not fid:
                        continue
                    files.append(
                        DriveFolderFile(
                            id=fid,
                            name=str(item.get("name") or fid),
                            mime_type=str(item.get("mimeType") or ""),
                        )
                    )
                page_token = data.get("nextPageToken")
                if not page_token:
                    break

        files.sort(key=lambda f: _natural_sort_key(f.name))
        return files

    def _used_drive_file_ids(self) -> set[str]:
        used: set[str] = set()
        for row in self.sheets.list_rows():
            url = (row.get("file_url") or "").strip()
            fid = extract_drive_file_id(url)
            if fid:
                used.add(fid)
        return used

    def _default_start_date(self, tz_name: str) -> date:
        try:
            today = datetime.now(ZoneInfo(tz_name)).date()
        except Exception:
            today = datetime.now(ZoneInfo("UTC")).date()

        latest: date | None = None
        for row in self.sheets.list_rows():
            status = (row.get("status") or "").strip().lower()
            if status not in ("", "pending"):
                continue
            when = parse_row_datetime(row, tz_name)
            if when is None:
                continue
            d = when.date()
            if d >= today and (latest is None or d > latest):
                latest = d

        if latest is None:
            return today
        return latest + timedelta(days=1)

    def scan_and_fill(
        self,
        *,
        folder: str | None = None,
        count: int | None = None,
        post_time: str | None = None,
        target: str | None = None,
        caption: str | None = None,
        start_date: str | None = None,
        caption_from_filename: bool | None = None,
        fill_empty: bool = False,
        weekends_only: bool = False,
    ) -> FolderScanResult:
        folder_raw = (folder or "").strip() or (
            self.settings.google_drive_content_folder_id or ""
        ).strip()
        folder_id = extract_drive_folder_id(folder_raw)
        if not folder_id:
            return FolderScanResult(
                ok=False,
                detail=(
                    "missing_folder_id: set GOOGLE_DRIVE_CONTENT_FOLDER_ID "
                    "or pass folder URL/id"
                ),
            )

        max_count = int(
            count
            if count is not None
            else getattr(self.settings, "content_scan_default_count", 30) or 30
        )
        max_count = max(1, min(max_count, 365))

        time_val = (
            (post_time or "").strip()
            or (getattr(self.settings, "content_scan_default_time", "") or "12:00").strip()
            or "12:00"
        )
        target_val = (
            (target or "").strip().lower()
            or (getattr(self.settings, "content_scan_default_target", "") or "both").strip().lower()
            or "both"
        )
        if target_val not in ("group", "channel", "both"):
            target_val = "both"

        default_caption = (
            caption
            if caption is not None
            else (getattr(self.settings, "content_scan_default_caption", "") or "")
        )
        use_filename_caption = (
            caption_from_filename
            if caption_from_filename is not None
            else bool(getattr(self.settings, "content_scan_caption_from_filename", True))
        )

        tz_name = (getattr(self.settings, "content_timezone", "") or "Asia/Karachi").strip()

        if (start_date or "").strip():
            try:
                start = datetime.strptime(start_date.strip()[:10], "%Y-%m-%d").date()
            except ValueError:
                return FolderScanResult(
                    ok=False,
                    folder_id=folder_id,
                    detail="bad_start_date: use YYYY-MM-DD",
                )
        else:
            start = self._default_start_date(tz_name)

        try:
            all_files = self.list_folder_files(folder_id)
        except Exception as exc:
            logger.exception("Drive folder list failed")
            return FolderScanResult(
                ok=False,
                folder_id=folder_id,
                detail=f"drive_list_failed: {exc}",
            )

        used = self._used_drive_file_ids()
        skipped_used = 0
        skipped_unsupported = 0
        candidates: list[tuple[DriveFolderFile, str]] = []

        for f in all_files:
            if f.id in used:
                skipped_used += 1
                continue
            media_type = detect_media_type(f.name, f.mime_type)
            if not media_type:
                skipped_unsupported += 1
                continue
            candidates.append((f, media_type))

        selected = candidates[:max_count]

        # Fill existing date rows that have no file_url (weekend template workflow)
        if fill_empty:
            empty_slots = [
                row
                for row in self.sheets.list_rows()
                if (row.get("date") or "").strip()
                and not (row.get("file_url") or "").strip()
            ]
            if not empty_slots:
                return FolderScanResult(
                    ok=True,
                    folder_id=folder_id,
                    scanned=len(all_files),
                    skipped_used=skipped_used,
                    skipped_unsupported=skipped_unsupported,
                    created=0,
                    detail="no_empty_file_url_slots",
                )
            to_fill = empty_slots[: len(selected)]
            filled: list[dict[str, Any]] = []
            try:
                for slot, (f, media_type) in zip(to_fill, selected):
                    if use_filename_caption and not (default_caption or "").strip():
                        cap = Path(f.name).stem
                    else:
                        cap = (default_caption or "").strip()
                    fields = {
                        "type": media_type,
                        "file_url": drive_file_url(f.id),
                        "caption": cap or (slot.get("caption") or ""),
                        "status": (slot.get("status") or "").strip() or "pending",
                        "notes": f"auto_scan_fill folder={folder_id} file={f.name}",
                    }
                    if not (slot.get("time") or "").strip():
                        fields["time"] = time_val
                    if not (slot.get("target") or "").strip():
                        fields["target"] = target_val
                    self.sheets.update_row_fields(int(slot["_row"]), fields)
                    item = dict(slot)
                    item.update(fields)
                    filled.append(item)
            except Exception as exc:
                logger.exception("ContentCalendar fill-empty failed")
                return FolderScanResult(
                    ok=False,
                    folder_id=folder_id,
                    scanned=len(all_files),
                    skipped_used=skipped_used,
                    skipped_unsupported=skipped_unsupported,
                    detail=f"sheet_fill_failed: {exc}",
                )
            return FolderScanResult(
                ok=True,
                folder_id=folder_id,
                scanned=len(all_files),
                skipped_used=skipped_used,
                skipped_unsupported=skipped_unsupported,
                created=len(filled),
                detail=(
                    f"filled_empty={len(filled)} "
                    f"time={time_val} target={target_val}"
                ),
                rows=filled,
            )

        if weekends_only:
            schedule_days = weekend_dates_from(start, len(selected))
        else:
            schedule_days = [start + timedelta(days=i) for i in range(len(selected))]

        rows_to_add: list[dict[str, str]] = []
        for i, (f, media_type) in enumerate(selected):
            day = schedule_days[i]
            if use_filename_caption and not (default_caption or "").strip():
                cap = Path(f.name).stem
            else:
                cap = (default_caption or "").strip()
            rows_to_add.append(
                {
                    "date": day.isoformat(),
                    "time": time_val,
                    "target": target_val,
                    "type": media_type,
                    "file_url": drive_file_url(f.id),
                    "caption": cap,
                    "status": "pending",
                    "posted_at": "",
                    "notes": f"auto_scan folder={folder_id} file={f.name}",
                }
            )

        if not rows_to_add:
            return FolderScanResult(
                ok=True,
                folder_id=folder_id,
                scanned=len(all_files),
                skipped_used=skipped_used,
                skipped_unsupported=skipped_unsupported,
                created=0,
                detail="no_new_files_to_schedule",
            )

        try:
            created_rows = self.sheets.append_pending_rows(rows_to_add)
        except Exception as exc:
            logger.exception("ContentCalendar append failed")
            return FolderScanResult(
                ok=False,
                folder_id=folder_id,
                scanned=len(all_files),
                skipped_used=skipped_used,
                skipped_unsupported=skipped_unsupported,
                detail=f"sheet_append_failed: {exc}",
            )

        weekend_note = " weekends_only" if weekends_only else ""
        return FolderScanResult(
            ok=True,
            folder_id=folder_id,
            scanned=len(all_files),
            skipped_used=skipped_used,
            skipped_unsupported=skipped_unsupported,
            created=len(created_rows),
            detail=(
                f"created={len(created_rows)} start={start.isoformat()} "
                f"time={time_val} target={target_val}{weekend_note}"
            ),
            rows=created_rows,
        )

"""
Google Sheet access for Sub-agent B ContentCalendar tab.

Columns:
  date | time | target | type | file_url | caption | status | posted_at | notes
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from app.config import Settings
from app.sheets import normalize_sheet_id

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

CONTENT_HEADERS = [
    "date",
    "time",
    "target",
    "type",
    "file_url",
    "caption",
    "status",
    "posted_at",
    "notes",
]

_STATUS_PENDING = "pending"


class ContentSheetsClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._worksheet = None

    def _load_credentials(self) -> Credentials:
        raw_json = (self.settings.google_service_account_json or "").strip()
        if raw_json:
            info = json.loads(raw_json)
            return Credentials.from_service_account_info(info, scopes=SCOPES)

        creds_path = Path(self.settings.google_service_account_file)
        if not creds_path.is_file():
            raise RuntimeError(
                "Google credentials missing. Set GOOGLE_SERVICE_ACCOUNT_JSON "
                "or provide GOOGLE_SERVICE_ACCOUNT_FILE."
            )
        return Credentials.from_service_account_file(str(creds_path), scopes=SCOPES)

    def _get_worksheet(self):
        if self._worksheet is not None:
            return self._worksheet

        sheet_id = normalize_sheet_id(self.settings.google_sheet_id)
        if not sheet_id:
            raise RuntimeError("GOOGLE_SHEET_ID is not set")

        credentials = self._load_credentials()
        client = gspread.authorize(credentials)
        spreadsheet = client.open_by_key(sheet_id)
        title = (
            (self.settings.google_sheet_content_worksheet or "ContentCalendar").strip()
            or "ContentCalendar"
        )
        try:
            worksheet = spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=title,
                rows=500,
                cols=len(CONTENT_HEADERS),
            )

        existing = worksheet.row_values(1)
        if not existing:
            worksheet.append_row(CONTENT_HEADERS, value_input_option="USER_ENTERED")
        else:
            lowered = [h.strip().lower() for h in existing]
            missing = [h for h in CONTENT_HEADERS if h.lower() not in lowered]
            if missing:
                start_col = len(existing) + 1
                end_col = start_col + len(missing) - 1
                worksheet.update(
                    f"R1C{start_col}:R1C{end_col}",
                    [missing],
                    value_input_option="USER_ENTERED",
                )

        self._worksheet = worksheet
        return worksheet

    def list_rows(self) -> list[dict]:
        """Return content rows as dicts with 1-based `_row` index."""
        worksheet = self._get_worksheet()
        values = worksheet.get_all_values()
        if len(values) < 2:
            return []
        header = [h.strip().lower() for h in values[0]]
        rows: list[dict] = []
        for i, raw in enumerate(values[1:], start=2):
            padded = list(raw) + [""] * max(0, len(header) - len(raw))
            item = {header[j]: padded[j] for j in range(len(header))}
            item["_row"] = i
            rows.append(item)
        return rows

    def list_pending_rows(self) -> list[dict]:
        rows = []
        for row in self.list_rows():
            status = (row.get("status") or "").strip().lower()
            if status == _STATUS_PENDING or status == "":
                # Empty status treated as pending so seeding is easy
                if status == "":
                    row["status"] = _STATUS_PENDING
                rows.append(row)
        return rows

    def mark_result(
        self,
        row_number: int,
        *,
        status: str,
        notes: str = "",
    ) -> None:
        worksheet = self._get_worksheet()
        header = [h.strip().lower() for h in worksheet.row_values(1)]

        def col(name: str, default: int) -> int:
            try:
                return header.index(name) + 1
            except ValueError:
                return default

        status_col = col("status", 7)
        posted_col = col("posted_at", 8)
        notes_col = col("notes", 9)
        posted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        worksheet.update_cell(row_number, status_col, status)
        worksheet.update_cell(row_number, posted_col, posted_at if status == "posted" else "")
        worksheet.update_cell(row_number, notes_col, notes[:500])
        logger.info("ContentCalendar row=%s status=%s", row_number, status)


def parse_row_datetime(row: dict, tz_name: str) -> datetime | None:
    """
    Build timezone-aware datetime from sheet date + time.
    Accepts date like 2026-09-22 or 22/09/2026; time like 10:00 or 10:00:00.
    """
    from zoneinfo import ZoneInfo

    date_raw = (row.get("date") or "").strip()
    time_raw = (row.get("time") or "00:00").strip() or "00:00"
    if not date_raw:
        return None

    date_part = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            date_part = datetime.strptime(date_raw, fmt).date()
            break
        except ValueError:
            continue
    if date_part is None:
        # Google Sheets sometimes serializes oddly; try first token
        token = re.split(r"\s+", date_raw)[0]
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                date_part = datetime.strptime(token, fmt).date()
                break
            except ValueError:
                continue
    if date_part is None:
        return None

    time_part = None
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M%p"):
        try:
            time_part = datetime.strptime(time_raw, fmt).time()
            break
        except ValueError:
            continue
    if time_part is None:
        return None

    try:
        tz = ZoneInfo(tz_name or "Asia/Karachi")
    except Exception:
        tz = ZoneInfo("UTC")

    return datetime.combine(date_part, time_part, tzinfo=tz)

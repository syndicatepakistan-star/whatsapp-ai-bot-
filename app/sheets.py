from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from app.config import Settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = ["timestamp", "name", "email", "phone", "status", "notes"]
_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def normalize_sheet_id(raw: str) -> str:
    """Accept either a bare ID or a full Google Sheets URL."""
    text = (raw or "").strip()
    if not text:
        return ""
    match = _SHEET_ID_RE.search(text)
    if match:
        return match.group(1)
    # Bare ID (no URL chars)
    if "/" not in text and " " not in text:
        return text
    return text


class GoogleSheetsClient:
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
        try:
            spreadsheet = client.open_by_key(sheet_id)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot open Google Sheet id={sheet_id!r}. "
                "Use only the ID from /d/SHEET_ID/edit, share the sheet with the "
                "service account client_email as Editor, and enable Sheets+Drive APIs. "
                f"Original error: {exc}"
            ) from exc

        try:
            worksheet = spreadsheet.worksheet(self.settings.google_sheet_worksheet)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=self.settings.google_sheet_worksheet,
                rows=1000,
                cols=len(HEADERS),
            )

        existing = worksheet.row_values(1)
        if not existing:
            worksheet.append_row(HEADERS, value_input_option="USER_ENTERED")

        self._worksheet = worksheet
        return worksheet

    def append_lead(
        self,
        *,
        name: str,
        email: str,
        phone: str,
        status: str,
        notes: str = "",
    ) -> None:
        worksheet = self._get_worksheet()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        worksheet.append_row(
            [timestamp, name, email, phone, status, notes],
            value_input_option="USER_ENTERED",
        )
        logger.info("Sheet row added status=%s phone=%s", status, phone)

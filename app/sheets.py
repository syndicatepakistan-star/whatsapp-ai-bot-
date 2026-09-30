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

HEADERS = [
    "timestamp",
    "name",
    "email",
    "phone",
    "status",
    "notes",
    "group_add_status",
    "group_add_detail",
    "diagnosis",
]

# Older sheets used group_add_dated — treat as alias of group_add_detail.
_HEADER_ALIASES = {
    "group_add_dated": "group_add_detail",
    "group_add_date": "group_add_detail",
    "group_add_notes": "group_add_detail",
}

BOOKING_HEADERS = [
    "timestamp",
    "name",
    "email",
    "phone",
    "status",
    "notes",
    "meet_link",
    "slot_start",
    "slot_end",
    "timezone",
    "booking_id",
    "reminder_sent",
]
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


def _digits_only(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def _normalize_phone_digits(raw: str) -> str:
    """Digits only, with common trunk-zero fixes (+44 07… → 447…)."""
    digits = _digits_only(raw)
    if not digits:
        return ""
    # UK national format stored as +44 0xxxxxxxxxx
    if digits.startswith("44") and len(digits) >= 12 and digits[2] == "0":
        digits = "44" + digits[3:]
    # North America sometimes stored with leading 0 after country code
    if digits.startswith("1") and len(digits) >= 12 and digits[1] == "0":
        digits = "1" + digits[2:]
    return digits


def _phones_match(a: str, b: str) -> bool:
    """Loose match: exact, strip +, or last 10 digits (ignores #ERROR! / formulas)."""
    left = (a or "").strip()
    right = (b or "").strip()
    if not left or not right:
        return False
    if left.upper() in {"#ERROR!", "#N/A", "#VALUE!", "#REF!"}:
        return False
    if right.upper() in {"#ERROR!", "#N/A", "#VALUE!", "#REF!"}:
        return False
    if left == right:
        return True
    if left.replace("+", "").replace(" ", "") == right.replace("+", "").replace(" ", ""):
        return True
    ld = _normalize_phone_digits(left)
    rd = _normalize_phone_digits(right)
    if not ld or not rd:
        return False
    if ld == rd:
        return True
    # Compare national significant number (last 10 digits)
    return len(ld) >= 10 and len(rd) >= 10 and ld[-10:] == rd[-10:]


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
        else:
            # Ensure newer Agent A columns exist on older sheets.
            lowered = [h.strip().lower() for h in existing]
            # Treat legacy aliases as already present (e.g. group_add_dated ≈ group_add_detail).
            covered = set(lowered)
            for alias, canonical in _HEADER_ALIASES.items():
                if alias in covered:
                    covered.add(canonical)
            missing = [h for h in HEADERS if h.lower() not in covered]
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

    def _get_bookings_worksheet(self):
        sheet_id = normalize_sheet_id(self.settings.google_sheet_id)
        if not sheet_id:
            raise RuntimeError("GOOGLE_SHEET_ID is not set")

        credentials = self._load_credentials()
        client = gspread.authorize(credentials)
        spreadsheet = client.open_by_key(sheet_id)
        title = (self.settings.google_sheet_bookings_worksheet or "Bookings").strip() or "Bookings"
        try:
            worksheet = spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=title,
                rows=1000,
                cols=len(BOOKING_HEADERS),
            )

        existing = worksheet.row_values(1)
        if not existing:
            worksheet.append_row(BOOKING_HEADERS, value_input_option="USER_ENTERED")
        return worksheet

    def append_lead(
        self,
        *,
        name: str,
        email: str,
        phone: str,
        status: str,
        notes: str = "",
        group_add_status: str = "",
        group_add_detail: str = "",
        diagnosis: str = "",
    ) -> None:
        worksheet = self._get_worksheet()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        # Map into header order (supports older sheets that only have first 6 cols).
        header = [h.strip().lower() for h in (worksheet.row_values(1) or HEADERS)]
        values_by_key = {
            "timestamp": timestamp,
            "name": name,
            "email": email,
            "phone": phone,
            "status": status,
            "notes": notes,
            "group_add_status": group_add_status,
            "group_add_detail": group_add_detail,
            "diagnosis": diagnosis,
        }
        # Support legacy header names (e.g. group_add_dated → group_add_detail).
        for alias, canonical in _HEADER_ALIASES.items():
            if alias in header and canonical in values_by_key:
                values_by_key[alias] = values_by_key[canonical]

        row = [values_by_key.get(h, "") for h in header]
        # If sheet somehow has no recognized headers, fall back to full HEADERS order.
        if not any(header):
            row = [values_by_key.get(h, "") for h in HEADERS]
        worksheet.append_row(row, value_input_option="USER_ENTERED")
        logger.info(
            "Sheet row added status=%s group_add=%s detail=%s diagnosis=%s phone=%s",
            status,
            group_add_status or "-",
            (group_add_detail or "-")[:80],
            diagnosis or "-",
            phone,
        )

    def update_lead_diagnosis(
        self,
        *,
        email: str = "",
        phone: str = "",
        diagnosis: str,
    ) -> bool:
        """
        Update diagnosis on ALL matching lead rows (by email and/or loose phone).
        Returns True if at least one row was updated.
        """
        diagnosis_norm = (diagnosis or "").strip()
        if not diagnosis_norm:
            return False

        email_norm = (email or "").strip().lower()
        phone_norm = (phone or "").strip()
        if not email_norm and not phone_norm:
            return False

        rows = self.list_lead_rows()
        matches: list[dict] = []
        for row in rows:
            row_email = (row.get("email") or "").strip().lower()
            row_phone = (row.get("phone") or "").strip()
            email_hit = bool(email_norm and row_email and row_email == email_norm)
            phone_hit = bool(phone_norm and row_phone and _phones_match(row_phone, phone_norm))
            if email_hit or phone_hit:
                matches.append(row)

        if not matches:
            return False

        worksheet = self._get_worksheet()
        header = [h.strip().lower() for h in worksheet.row_values(1)]
        try:
            diagnosis_col = header.index("diagnosis") + 1
        except ValueError:
            start_col = len(header) + 1
            worksheet.update(
                f"R1C{start_col}:R1C{start_col}",
                [["diagnosis"]],
                value_input_option="USER_ENTERED",
            )
            diagnosis_col = start_col

        for match in matches:
            worksheet.update_cell(int(match["_row"]), diagnosis_col, diagnosis_norm)
            logger.info(
                "Sheet diagnosis updated row=%s diagnosis=%s email=%s phone=%s",
                match["_row"],
                diagnosis_norm,
                email_norm or "-",
                phone_norm or "-",
            )
        return True

    def list_lead_rows(self) -> list[dict]:
        """Return lead rows as dicts with 1-based `_row` index."""
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

    def update_lead_group_status(
        self,
        row_number: int,
        *,
        group_add_status: str,
        group_add_detail: str = "",
    ) -> None:
        worksheet = self._get_worksheet()
        header = [h.strip().lower() for h in worksheet.row_values(1)]

        def _col(*names: str, default: int) -> int:
            for name in names:
                try:
                    return header.index(name) + 1
                except ValueError:
                    continue
            return default

        status_col = _col("group_add_status", default=7)
        detail_col = _col(
            "group_add_detail",
            "group_add_dated",
            "group_add_date",
            "group_add_notes",
            default=8,
        )
        worksheet.update_cell(row_number, status_col, group_add_status)
        worksheet.update_cell(row_number, detail_col, group_add_detail)

    def append_booking(
        self,
        *,
        name: str,
        email: str,
        phone: str,
        status: str,
        notes: str = "",
        meet_link: str = "",
        slot_start: str = "",
        slot_end: str = "",
        timezone_name: str = "",
        booking_id: str = "",
        reminder_sent: str = "no",
    ) -> None:
        worksheet = self._get_bookings_worksheet()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        worksheet.append_row(
            [
                timestamp,
                name,
                email,
                phone,
                status,
                notes,
                meet_link,
                slot_start,
                slot_end,
                timezone_name,
                booking_id,
                reminder_sent,
            ],
            value_input_option="USER_ENTERED",
        )
        logger.info("Booking sheet row added status=%s phone=%s", status, phone)

    def list_booking_rows(self) -> list[dict]:
        """Return booking rows as dicts (1-based sheet row index included)."""
        worksheet = self._get_bookings_worksheet()
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

    def mark_booking_reminder_sent(self, row_number: int) -> None:
        worksheet = self._get_bookings_worksheet()
        # reminder_sent is column L (12)
        worksheet.update_cell(row_number, 12, "yes")

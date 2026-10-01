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
    "quiz_followup_status",
    "quiz_followup_detail",
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


_EMAIL_RE = re.compile(
    r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}",
    re.IGNORECASE,
)
_EMAIL_QUERY_RE = re.compile(
    r"(?:email|e)=([A-Z0-9._%+\-]+(?:%40|@)[A-Z0-9.\-]+\.[A-Z]{2,})",
    re.IGNORECASE,
)


def _digits_only(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def _normalize_email(raw: str) -> str:
    return (raw or "").strip().lower().replace(" ", "").replace("%40", "@")


def _emails_from_text(raw: str) -> set[str]:
    """Collect emails from free text / intake URLs (including email=%40 encoding)."""
    text = (raw or "").strip()
    if not text:
        return set()
    found: set[str] = set()
    for match in _EMAIL_QUERY_RE.findall(text):
        normalized = _normalize_email(match)
        if normalized:
            found.add(normalized)
    for match in _EMAIL_RE.findall(text.replace("%40", "@")):
        normalized = _normalize_email(match)
        if normalized:
            found.add(normalized)
    return found


def _row_emails(row: dict) -> set[str]:
    """Emails for a sheet row: email column + any emails embedded in notes."""
    emails = set()
    primary = _normalize_email(row.get("email") or "")
    if primary:
        emails.add(primary)
    emails |= _emails_from_text(row.get("notes") or "")
    return emails


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


def _find_diagnosis_matches(
    rows: list[dict],
    *,
    email: str = "",
    phone: str = "",
) -> list[dict]:
    """
    Match Leads rows for diagnosis updates.

    Priority:
      1) Email (sheet email column OR email inside notes/intake URL)
      2) Phone only when the row has no conflicting different email
    """
    email_norm = _normalize_email(email)
    phone_norm = (phone or "").strip()
    if not email_norm and not phone_norm:
        return []

    matches: list[dict] = []
    seen_rows: set[int] = set()

    if email_norm:
        for row in rows:
            row_id = int(row.get("_row") or 0)
            if row_id in seen_rows:
                continue
            if email_norm in _row_emails(row):
                matches.append(row)
                seen_rows.add(row_id)

    if phone_norm:
        for row in rows:
            row_id = int(row.get("_row") or 0)
            if row_id in seen_rows:
                continue
            row_phone = (row.get("phone") or "").strip()
            if not (row_phone and _phones_match(row_phone, phone_norm)):
                continue
            row_emails = _row_emails(row)
            # Do not stamp diagnosis onto another person's row via loose phone match.
            if email_norm and row_emails and email_norm not in row_emails:
                continue
            matches.append(row)
            seen_rows.add(row_id)

    return matches


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

    def _expand_worksheet_cols(self, worksheet, min_cols: int) -> None:
        """Google Sheets rejects writes past the current grid; grow columns first."""
        needed = max(1, int(min_cols))
        current = int(getattr(worksheet, "col_count", 0) or 0)
        if current >= needed:
            return
        rows = max(int(getattr(worksheet, "row_count", 0) or 0), 1)
        worksheet.resize(rows=rows, cols=needed)
        logger.info(
            "Expanded sheet %r grid cols %s → %s",
            getattr(worksheet, "title", "?"),
            current,
            needed,
        )

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
                cols=max(len(HEADERS) + 4, 16),
            )

        existing = worksheet.row_values(1)
        if not existing:
            self._expand_worksheet_cols(worksheet, len(HEADERS))
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
                self._expand_worksheet_cols(worksheet, end_col)
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
        Update diagnosis on ALL matching lead rows.
        Email (column or notes/intake URL) wins; phone is fallback only.
        Returns True if at least one row was updated.
        """
        diagnosis_norm = (diagnosis or "").strip()
        if not diagnosis_norm:
            return False

        email_norm = _normalize_email(email)
        phone_norm = (phone or "").strip()
        if not email_norm and not phone_norm:
            return False

        matches = _find_diagnosis_matches(
            self.list_lead_rows(),
            email=email_norm,
            phone=phone_norm,
        )
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

    def batch_update_diagnoses(
        self,
        items: list[dict],
    ) -> dict:
        """
        Apply many diagnosis updates with ONE sheet read + batched cell writes.

        Each item: {email?, phone?, diagnosis}
        Returns {updated, skipped, rows_touched, skipped_items:[{email,phone,diagnosis}]}
        """
        worksheet = self._get_worksheet()
        rows = self.list_lead_rows()
        header = [h.strip().lower() for h in (worksheet.row_values(1) or [])]
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
            header = [h.strip().lower() for h in (worksheet.row_values(1) or [])]

        updated = 0
        skipped = 0
        rows_touched = 0
        skipped_items: list[dict] = []
        # Collect (row_number, value) then write in chunks.
        writes: list[tuple[int, str]] = []

        for raw in items:
            if not isinstance(raw, dict):
                skipped += 1
                continue
            diagnosis_norm = (raw.get("diagnosis") or "").strip()
            if diagnosis_norm.lower() == "completed":
                diagnosis_norm = "Completed"
            elif diagnosis_norm:
                diagnosis_norm = "Not Completed"
            else:
                skipped += 1
                continue

            email_norm = _normalize_email(raw.get("email") or "")
            phone_norm = (raw.get("phone") or "").strip()
            matches = _find_diagnosis_matches(
                rows,
                email=email_norm,
                phone=phone_norm,
            )

            if not matches:
                skipped += 1
                skipped_items.append(
                    {
                        "email": email_norm,
                        "phone": phone_norm,
                        "diagnosis": diagnosis_norm,
                    }
                )
                continue

            updated += 1
            for match in matches:
                writes.append((int(match["_row"]), diagnosis_norm))
                rows_touched += 1

        # Batch write (Google Sheets API) — chunks of 100 cells.
        for i in range(0, len(writes), 100):
            chunk = writes[i : i + 100]
            data = [
                {
                    "range": f"R{row_num}C{diagnosis_col}",
                    "values": [[value]],
                }
                for row_num, value in chunk
            ]
            if data:
                worksheet.batch_update(data, value_input_option="USER_ENTERED")

        logger.info(
            "Batch diagnosis done updated_users=%s skipped_users=%s rows_touched=%s",
            updated,
            skipped,
            rows_touched,
        )
        return {
            "updated": updated,
            "skipped": skipped,
            "rows_touched": rows_touched,
            "skipped_items": skipped_items[:50],  # cap response size
        }

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

    def ensure_lead_columns(self, *column_names: str) -> None:
        """Append missing header names to row 1 (does not shift existing cells)."""
        worksheet = self._get_worksheet()
        header = [h.strip() for h in (worksheet.row_values(1) or [])]
        header_lower = {h.strip().lower() for h in header if h.strip()}
        to_add = [
            name
            for name in column_names
            if name and name.strip().lower() not in header_lower
        ]
        if not to_add:
            return
        start_col = len(header) + 1
        end_col = start_col + len(to_add) - 1
        self._expand_worksheet_cols(worksheet, end_col)
        worksheet.update(
            f"R1C{start_col}:R1C{end_col}",
            [to_add],
            value_input_option="USER_ENTERED",
        )
        logger.info("Added Leads headers: %s", ", ".join(to_add))

    def update_lead_quiz_followup(
        self,
        row_number: int,
        *,
        status: str,
        detail: str = "",
    ) -> None:
        self.ensure_lead_columns("quiz_followup_status", "quiz_followup_detail")
        worksheet = self._get_worksheet()
        header = [h.strip().lower() for h in worksheet.row_values(1)]

        def _col(name: str) -> int:
            return header.index(name) + 1

        # Re-read after ensure (header may have grown).
        header = [h.strip().lower() for h in worksheet.row_values(1)]
        status_col = _col("quiz_followup_status")
        detail_col = _col("quiz_followup_detail")
        worksheet.update_cell(row_number, status_col, status)
        worksheet.update_cell(row_number, detail_col, (detail or "")[:500])

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

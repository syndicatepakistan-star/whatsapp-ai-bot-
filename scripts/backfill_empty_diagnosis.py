"""
Fill remaining empty diagnosis cells on the Leads sheet.

Use after Django backfill_lead_diagnosis. Rows that never matched a Django user
(extra sheet numbers, #ERROR! phones, typos) stay blank — this script can set
them to "Not Completed" (or a custom value).

Usage (from WhatsApp bot repo root / Railway bot shell):
  python scripts/backfill_empty_diagnosis.py --dry-run
  python scripts/backfill_empty_diagnosis.py
  python scripts/backfill_empty_diagnosis.py --value "Not Completed"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.sheets import GoogleSheetsClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill empty diagnosis cells on Leads")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--value",
        default="Not Completed",
        help='Value to write into empty diagnosis cells (default "Not Completed")',
    )
    parser.add_argument("--limit", type=int, default=0, help="Max cells to fill (0 = all)")
    args = parser.parse_args()

    value = (args.value or "").strip()
    if value not in {"Completed", "Not Completed"}:
        print('Use --value "Completed" or "Not Completed"')
        return 1

    sheets = GoogleSheetsClient(get_settings())
    rows = sheets.list_lead_rows()
    worksheet = sheets._get_worksheet()
    header = [h.strip().lower() for h in worksheet.row_values(1)]
    try:
        diagnosis_col = header.index("diagnosis") + 1
    except ValueError:
        print("No diagnosis column on sheet — add header first.")
        return 1

    filled = 0
    skipped = 0
    for row in rows:
        if args.limit and filled >= args.limit:
            break
        current = (row.get("diagnosis") or "").strip()
        if current:
            skipped += 1
            continue
        row_number = int(row["_row"])
        email = (row.get("email") or "").strip()
        phone = (row.get("phone") or "").strip()
        label = f"row={row_number} {email or '-'} {phone or '-'}"
        if args.dry_run:
            print(f"DRY-RUN {label} → {value}")
        else:
            worksheet.update_cell(row_number, diagnosis_col, value)
            print(f"OK {label} → {value}")
        filled += 1

    print(f"\nDone filled={filled} already_set={skipped} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

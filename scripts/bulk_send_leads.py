"""
Bulk-send WhatsApp messages from a CSV export (Django admin quiz users export).

Usage (from repo root):
  python scripts/bulk_send_leads.py --csv leads.csv
  python scripts/bulk_send_leads.py --csv leads.csv --dry-run --limit 5

CSV columns (header row required):
  User Name, Email, Number, Intake URL
  (matches Syndicate admin export; also accepts name, email, phone, intake_url)
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.workflow import LeadWorkflow  # noqa: E402

COLUMN_ALIASES = {
    "name": ("user name", "name"),
    "email": ("email",),
    "phone": ("number", "phone"),
    "intake_url": ("intake url", "intake_url"),
}


def _normalize_header(value: str) -> str:
    return (value or "").strip().lower()


def _row_value(row: dict[str, str], field: str) -> str:
    for alias in COLUMN_ALIASES[field]:
        for key, val in row.items():
            if _normalize_header(key) == alias:
                return (val or "").strip()
    return ""


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for raw in reader:
            rows.append(
                {
                    "name": _row_value(raw, "name"),
                    "email": _row_value(raw, "email"),
                    "phone": _row_value(raw, "phone"),
                    "intake_url": _row_value(raw, "intake_url"),
                }
            )
        return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk send quiz leads via Whapi bot")
    parser.add_argument("--csv", required=True, help="Path to CSV export")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=4.0)
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        raise SystemExit(f"CSV not found: {csv_path}")

    rows = [r for r in load_rows(csv_path) if r["phone"]]
    if args.limit:
        rows = rows[: args.limit]

    if not rows:
        raise SystemExit("No rows with phone found in CSV.")

    settings = get_settings()
    workflow = LeadWorkflow(settings)

    print(f"Processing {len(rows)} lead(s). delay={args.delay}s dry_run={args.dry_run}")

    for index, row in enumerate(rows, start=1):
        label = f"[{index}/{len(rows)}] {row['name']} {row['phone']}"
        if args.dry_run:
            print(f"DRY-RUN {label} -> {row['intake_url']}")
            continue

        result = workflow.process(
            name=row["name"],
            email=row["email"],
            phone=row["phone"],
            intake_url=row["intake_url"],
        )
        print(f"{result.status.upper()} {label} ({result.detail})")

        if index < len(rows) and args.delay > 0:
            time.sleep(args.delay)

    print("Done.")


if __name__ == "__main__":
    main()

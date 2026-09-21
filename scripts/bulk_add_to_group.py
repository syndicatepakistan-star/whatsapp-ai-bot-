"""
Bulk Sub-agent A: add existing sheet leads to the WhatsApp group.

Usage (from repo root):
  python scripts/bulk_add_to_group.py --dry-run --limit 5
  python scripts/bulk_add_to_group.py --limit 20
  python scripts/bulk_add_to_group.py

Skips rows that already have group_add_status in: added, invite_sent
Only processes rows whose lead status looks like WhatsApp was sent.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.community_agent import CommunityAgent  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.sheets import GoogleSheetsClient  # noqa: E402

ALREADY_DONE = {"added", "invite_sent"}
ELIGIBLE_STATUS = {
    "whatsapp message sent",
    "whatsapp_sent",
    "sent",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk add sheet leads to WhatsApp group")
    parser.add_argument("--dry-run", action="store_true", help="Do not call Whapi / update sheet")
    parser.add_argument("--limit", type=int, default=0, help="Max rows to process (0 = all)")
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds between adds")
    args = parser.parse_args()

    settings = get_settings()
    if not (settings.whapi_group_id or "").strip():
        print("Set WHAPI_GROUP_ID first (run scripts/list_whapi_groups.py).")
        return 1

    sheets = GoogleSheetsClient(settings)
    agent = CommunityAgent(settings)
    rows = sheets.list_lead_rows()

    processed = 0
    added = 0
    invited = 0
    failed = 0
    skipped = 0

    for row in rows:
        if args.limit and processed >= args.limit:
            break

        status = (row.get("status") or "").strip().lower()
        group_status = (row.get("group_add_status") or "").strip().lower()
        phone = (row.get("phone") or "").strip()
        name = (row.get("name") or "").strip()
        row_number = int(row.get("_row") or 0)

        if group_status in ALREADY_DONE:
            skipped += 1
            continue
        if status and status not in ELIGIBLE_STATUS:
            # Still allow empty/unknown if phone exists and WA was likely sent historically
            if "whatsapp" not in status and status not in {"ok", ""}:
                skipped += 1
                continue
        if not phone:
            skipped += 1
            continue

        processed += 1
        print(f"[{processed}] row={row_number} phone={phone} name={name!r} ...", end=" ")

        if args.dry_run:
            print("DRY-RUN")
            continue

        result = agent.add_lead_to_group(name=name, e164_phone=phone)
        print(f"{result.status} ({result.detail})")

        try:
            sheets.update_lead_group_status(
                row_number,
                group_add_status=result.status,
                group_add_detail=result.detail,
            )
        except Exception as exc:
            print(f"  sheet update failed: {exc}")

        if result.status == "added":
            added += 1
        elif result.status == "invite_sent":
            invited += 1
        else:
            failed += 1

        time.sleep(max(0.0, args.delay))

    print(
        f"\nDone processed={processed} added={added} invite_sent={invited} "
        f"failed={failed} skipped={skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

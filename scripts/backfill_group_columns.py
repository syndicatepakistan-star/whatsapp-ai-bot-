"""
Backfill empty group_add_status / group_add_detail on Leads sheet rows.

Two modes:
  1) Default: fill "skipped" + reason from the lead status (no WhatsApp calls)
  2) --add-to-group: for rows with WhatsApp sent, run Sub-agent A group add

Usage (from WhatsApp bot repo root):
  python scripts/backfill_group_columns.py --dry-run
  python scripts/backfill_group_columns.py
  python scripts/backfill_group_columns.py --add-to-group --limit 20
  python scripts/backfill_group_columns.py --add-to-group --dry-run --limit 5
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

ALREADY_DONE = {"added", "invite_sent", "failed", "skipped"}
ELIGIBLE_FOR_GROUP_ADD = {
    "whatsapp message sent",
    "whatsapp_sent",
    "sent",
}

# Lead status → (group_add_status, group_add_detail) when group cols are empty.
SKIP_FROM_STATUS = {
    "wrong number": ("skipped", "wrong_number"),
    "manual follow-up needed": ("skipped", "not_on_whatsapp"),
    "manual follow up needed": ("skipped", "not_on_whatsapp"),
    "whatsapp send failed": ("skipped", "whatsapp_send_failed"),
    "whatsapp_send_failed": ("skipped", "whatsapp_send_failed"),
}


def _normalize_phone_key(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill group_add_status / group_add_detail on Leads sheet"
    )
    parser.add_argument("--dry-run", action="store_true", help="Print only; do not update sheet")
    parser.add_argument("--limit", type=int, default=0, help="Max rows to change (0 = all)")
    parser.add_argument("--delay", type=float, default=1.5, help="Delay between group-add calls")
    parser.add_argument(
        "--add-to-group",
        action="store_true",
        help="Also try Whapi group add for rows with WhatsApp sent + empty group cols",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing group_add_status (except use carefully)",
    )
    args = parser.parse_args()

    settings = get_settings()
    sheets = GoogleSheetsClient(settings)
    rows = sheets.list_lead_rows()

    skip_filled = 0
    group_added = 0
    group_invited = 0
    group_failed = 0
    unchanged = 0
    errors = 0
    changed = 0

    agent = CommunityAgent(settings) if args.add_to_group else None
    if args.add_to_group and not (settings.whapi_group_id or "").strip():
        print("Set WHAPI_GROUP_ID before using --add-to-group.")
        return 1

    for row in rows:
        if args.limit and changed >= args.limit:
            break

        row_number = int(row.get("_row") or 0)
        status = (row.get("status") or "").strip().lower()
        group_status = (row.get("group_add_status") or "").strip().lower()
        # Legacy header alias may appear as group_add_dated in sheet dict keys.
        group_detail = (
            row.get("group_add_detail")
            or row.get("group_add_dated")
            or row.get("group_add_date")
            or ""
        ).strip()
        phone = (row.get("phone") or "").strip()
        name = (row.get("name") or "").strip()
        email = (row.get("email") or "").strip()

        if group_status and not args.force:
            if group_status in ALREADY_DONE or group_detail:
                unchanged += 1
                continue

        # 1) Fill skipped from known lead statuses (no WhatsApp).
        mapped = SKIP_FROM_STATUS.get(status)
        if mapped and (args.force or not group_status):
            new_status, new_detail = mapped
            label = (
                f"row={row_number} {email or '-'} {phone or '-'} "
                f"status={status!r} → {new_status}/{new_detail}"
            )
            if args.dry_run:
                print(f"DRY-RUN skip-fill {label}")
            else:
                try:
                    sheets.update_lead_group_status(
                        row_number,
                        group_add_status=new_status,
                        group_add_detail=new_detail,
                    )
                    print(f"OK skip-fill {label}")
                except Exception as exc:
                    errors += 1
                    print(f"FAIL skip-fill {label} err={exc}")
                    continue
            skip_filled += 1
            changed += 1
            continue

        # 2) Optional: real group add for WhatsApp-sent rows.
        if not args.add_to_group:
            unchanged += 1
            continue

        if status not in ELIGIBLE_FOR_GROUP_ADD and "whatsapp message sent" not in status:
            unchanged += 1
            continue
        if not phone or not _normalize_phone_key(phone):
            unchanged += 1
            continue
        if group_status in {"added", "invite_sent"} and not args.force:
            unchanged += 1
            continue

        label = f"row={row_number} {name!r} {phone} status={status!r}"
        if args.dry_run:
            print(f"DRY-RUN group-add {label}")
            changed += 1
            continue

        assert agent is not None
        result = agent.add_lead_to_group(name=name, e164_phone=phone)
        try:
            sheets.update_lead_group_status(
                row_number,
                group_add_status=result.status,
                group_add_detail=result.detail,
            )
            print(f"OK group-add {label} → {result.status} ({result.detail})")
        except Exception as exc:
            errors += 1
            print(f"FAIL group-add sheet {label} err={exc}")
            continue

        if result.status == "added":
            group_added += 1
        elif result.status == "invite_sent":
            group_invited += 1
        else:
            group_failed += 1
        changed += 1
        time.sleep(max(0.0, args.delay))

    print(
        "\nDone "
        f"changed={changed} skip_filled={skip_filled} "
        f"added={group_added} invite_sent={group_invited} "
        f"group_failed={group_failed} unchanged={unchanged} errors={errors} "
        f"dry_run={args.dry_run}"
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

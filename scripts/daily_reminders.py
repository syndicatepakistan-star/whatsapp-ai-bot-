"""
Run daily quiz / audit funnel reminders once.

Usage (repo root or Railway console):
  python scripts/daily_reminders.py --dry-run
  python scripts/daily_reminders.py --dry-run --limit 5
  python scripts/daily_reminders.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.daily_reminders import DailyReminderService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily quiz/audit WhatsApp reminders")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Max people this run (0 = config default)")
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument(
        "--skip-whatsapp-check",
        action="store_true",
        help="Skip Whapi contact check before send",
    )
    args = parser.parse_args()

    result = DailyReminderService(get_settings()).run(
        dry_run=bool(args.dry_run),
        limit=int(args.limit or 0),
        delay=float(args.delay),
        check_whatsapp=not bool(args.skip_whatsapp_check),
    )

    for item in result.details or []:
        print(
            f"{item.get('action')} kind={item.get('kind') or '-'} "
            f"row={item.get('row')} {item.get('email')} "
            f"→ {item.get('detail') or item.get('link') or item.get('message_id') or '-'}"
        )

    print(
        f"\nDone quiz_sent={result.quiz_sent} audit_sent={result.audit_sent} "
        f"skipped={result.skipped} failed={result.failed} dry_run={result.dry_run} "
        f"detail={result.detail}"
    )
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""
Message Leads sheet rows where diagnosis = Not Completed (after delay).

Default: only status=lead captured, older than QUIZ_FOLLOWUP_DELAY_MINUTES (10).

Usage (Bot A container):
  python scripts/message_not_completed.py --dry-run
  python scripts/message_not_completed.py --dry-run --limit 5
  python scripts/message_not_completed.py --limit 10
  python scripts/message_not_completed.py --min-age-minutes 0   # ignore age (manual blast)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.quiz_followup import QuizFollowupService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="WhatsApp follow-up for diagnosis=Not Completed leads"
    )
    parser.add_argument("--dry-run", action="store_true", help="Print only; no WhatsApp / sheet writes")
    parser.add_argument("--limit", type=int, default=0, help="Max Not Completed rows to process (0 = all)")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between sends")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-send even if quiz_followup_status already set",
    )
    parser.add_argument(
        "--include-manual-followup",
        action="store_true",
        help="Also try rows previously marked manual follow-up needed",
    )
    parser.add_argument(
        "--skip-whatsapp-check",
        action="store_true",
        help="Do not call Whapi contact check before send",
    )
    parser.add_argument(
        "--min-age-minutes",
        type=int,
        default=None,
        help="Only rows older than this (default = QUIZ_FOLLOWUP_DELAY_MINUTES). Use 0 to ignore age.",
    )
    parser.add_argument(
        "--all-statuses",
        action="store_true",
        help="Do not require status=lead captured (legacy rows)",
    )
    args = parser.parse_args()

    settings = get_settings()
    service = QuizFollowupService(settings)
    result = service.run(
        dry_run=bool(args.dry_run),
        limit=int(args.limit or 0),
        delay=float(args.delay),
        force=bool(args.force),
        include_manual_followup=bool(args.include_manual_followup),
        check_whatsapp=not bool(args.skip_whatsapp_check),
        min_age_minutes=args.min_age_minutes,
        only_lead_captured=not bool(args.all_statuses),
    )

    for item in result.details or []:
        print(
            f"{item.get('action')} row={item.get('row')} "
            f"{item.get('email')} {item.get('phone')} "
            f"→ {item.get('detail') or item.get('quiz_url') or '-'}"
        )

    print(
        f"\nDone sent={result.sent} skipped={result.skipped} "
        f"failed={result.failed} dry_run={result.dry_run}"
    )
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

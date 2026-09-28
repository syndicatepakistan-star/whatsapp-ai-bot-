"""
Scan Drive content folder → append pending ContentCalendar rows.

Usage (from repo root):
  python -m sub_agent_b.scan_folder
  python -m sub_agent_b.scan_folder --count 30 --time 12:00 --target both
  python -m sub_agent_b.scan_folder --folder "https://drive.google.com/drive/folders/XXXX"
  python -m sub_agent_b.scan_folder --start-date 2026-09-25
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from sub_agent_b.folder_scan import FolderScanner  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan Drive folder and create ContentCalendar pending rows"
    )
    parser.add_argument(
        "--folder",
        default="",
        help="Drive folder URL or ID (default: GOOGLE_DRIVE_CONTENT_FOLDER_ID)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="Max new rows to create (default: CONTENT_SCAN_DEFAULT_COUNT / 30)",
    )
    parser.add_argument("--time", default="", help="Daily post time HH:MM (default 12:00)")
    parser.add_argument(
        "--target",
        default="",
        choices=["", "group", "channel", "both"],
        help="Post target (default both)",
    )
    parser.add_argument("--caption", default="", help="Fixed caption for all rows")
    parser.add_argument(
        "--start-date",
        default="",
        help="First date YYYY-MM-DD (default: day after last pending, or today)",
    )
    parser.add_argument(
        "--no-filename-caption",
        action="store_true",
        help="Do not use filename as caption when caption is empty",
    )
    args = parser.parse_args()

    settings = get_settings()
    result = FolderScanner(settings).scan_and_fill(
        folder=(args.folder or "").strip() or None,
        count=args.count,
        post_time=(args.time or "").strip() or None,
        target=(args.target or "").strip() or None,
        caption=args.caption if args.caption else None,
        start_date=(args.start_date or "").strip() or None,
        caption_from_filename=False if args.no_filename_caption else None,
    )
    print(
        json.dumps(
            {
                "ok": result.ok,
                "folder_id": result.folder_id,
                "scanned": result.scanned,
                "skipped_used": result.skipped_used,
                "skipped_unsupported": result.skipped_unsupported,
                "created": result.created,
                "detail": result.detail,
                "rows": [
                    {
                        "row": r.get("_row"),
                        "date": r.get("date"),
                        "time": r.get("time"),
                        "type": r.get("type"),
                        "file_url": r.get("file_url"),
                        "caption": r.get("caption"),
                    }
                    for r in result.rows
                ],
            },
            indent=2,
        )
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

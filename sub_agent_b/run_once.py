"""
Run Sub-agent B once locally (same logic as /cron/content-posts).

Usage (from repo root):
  python -m sub_agent_b.run_once
  python sub_agent_b/run_once.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from sub_agent_b.poster import ContentPoster  # noqa: E402


def main() -> int:
    settings = get_settings()
    result = ContentPoster(settings).run_due()
    print(
        json.dumps(
            {
                "ok": result.ok,
                "processed": result.processed,
                "posted": result.posted,
                "failed": result.failed,
                "skipped": result.skipped,
                "detail": result.detail,
                "details": result.details,
            },
            indent=2,
        )
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

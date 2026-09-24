"""
List WhatsApp channels (newsletters) to get WHAPI_CHANNEL_ID.

Usage (from repo root):
  python -m sub_agent_b.list_channels
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from sub_agent_b.media_whapi import MediaWhapiClient  # noqa: E402


def main() -> int:
    settings = get_settings()
    if not (settings.whapi_token or "").strip():
        print("WHAPI_TOKEN is missing in .env")
        return 1

    result = MediaWhapiClient(settings).list_newsletters(count=100, offset=0)
    print(json.dumps({k: v for k, v in result.items() if k != "raw"}, indent=2, default=str))
    # Print without huge raw blobs
    channels = result.get("channels") or []
    clean = []
    for c in channels:
        clean.append({"id": c.get("id"), "name": c.get("name")})
    print("\n=== Copy one of these into WHAPI_CHANNEL_ID ===")
    for c in clean:
        print(f"- {c.get('name')}: {c.get('id')}")
    if not clean:
        print("(none found — create/open the channel on the Whapi phone, then retry)")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

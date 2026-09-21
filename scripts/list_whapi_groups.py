"""
List WhatsApp groups for the linked Whapi channel (fetch Group ID).

Usage (from repo root):
  python scripts/list_whapi_groups.py

Copy the `id` that ends with @g.us into Railway as WHAPI_GROUP_ID.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.whatsapp import WhatsAppClient  # noqa: E402


def main() -> int:
    settings = get_settings()
    if not (settings.whapi_token or "").strip():
        print("WHAPI_TOKEN is missing in .env")
        return 1

    result = WhatsAppClient(settings).list_groups(count=100, offset=0)
    print(json.dumps(result, indent=2))
    if not result.get("ok"):
        return 1

    groups = result.get("groups") or []
    if not groups:
        print(
            "\nNo groups found. Create a group on the Whapi phone, "
            "add this WhatsApp number as admin, then re-run."
        )
        return 0

    print("\n=== Copy one of these into WHAPI_GROUP_ID ===")
    for g in groups:
        print(f"- {g.get('name')}: {g.get('id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

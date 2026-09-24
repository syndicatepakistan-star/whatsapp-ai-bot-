"""Railway-safe process entrypoint (reads PORT from the environment)."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    raw = os.environ.get("PORT") or "8080"
    port = int(raw)
    print(f"Starting uvicorn host=0.0.0.0 port={port} (PORT env={os.environ.get('PORT')!r})", flush=True)
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()

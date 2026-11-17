"""Verify the public Mini App separately from the bot/container health check."""

import json
import os
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import urlopen


def check_public_app(origin: str) -> None:
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
        raise ValueError("Set ARCHIVE_DOMAIN or an HTTPS ARCHIVE_PUBLIC_URL")
    origin = origin.rstrip("/")

    def read(path):
        with urlopen(origin + path, timeout=15) as response:
            if urlsplit(response.url).netloc != parsed.netloc:
                raise ValueError("Mini App redirected to another host")
            return response.read(2 * 1024 * 1024)

    if json.loads(read("/healthz")).get("ok") is not True:
        raise ValueError("Public archive health check failed")
    if b"/assets/app.js" not in read("/"):
        raise ValueError("Mini App HTML is unavailable")
    if b"/api/events" not in read("/assets/app.js"):
        raise ValueError("Mini App JavaScript is unavailable")
    try:
        read("/api/session")
    except HTTPError as error:
        with error:
            if error.code != 401:
                raise ValueError("Archive authentication check failed") from error
    else:
        raise ValueError("Archive API is accessible without authentication")


def main() -> int:
    domain = os.getenv("ARCHIVE_DOMAIN", "").strip()
    origin = os.getenv("ARCHIVE_PUBLIC_URL", "").strip() or (f"https://{domain}" if domain else "")
    try:
        check_public_app(origin)
    except Exception as error:
        print(f"Public Mini App check failed ({type(error).__name__}). Check HTTPS, DNS and Caddy.", file=sys.stderr)
        return 1
    print("Public Mini App: HTTPS, HTML, JavaScript and API authentication are available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

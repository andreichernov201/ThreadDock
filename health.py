"""Docker health check: local heartbeat, no Bot API requests."""

import sqlite3
import time

from config import load_settings


def main() -> int:
    path = load_settings(require_token=False).data_dir / "threaddock.sqlite3"
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1) as database:
            row = database.execute("SELECT value FROM state WHERE key='heartbeat'").fetchone()
        return 0 if row and 0 <= time.time() - float(row[0]) < 60 else 1
    except OSError, sqlite3.Error, ValueError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

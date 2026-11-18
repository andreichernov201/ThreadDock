"""Export only Instagram cookies from an explicitly selected Firefox/Zen profile.

Run manually after logging into the dedicated account. No passwords, browser
automation, external requests or unrelated-site cookies are involved.
"""

import argparse
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path


def export_profile(profile: Path, destination: Path) -> int:
    database = (profile / "cookies.sqlite").resolve()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            "SELECT host,path,isSecure,expiry,name,value FROM moz_cookies "
            "WHERE host='instagram.com' OR host LIKE '%.instagram.com'"
        ).fetchall()
    jar = MozillaCookieJar()
    for host, path, secure, expiry, name, value in rows:
        if expiry and expiry <= time.time():
            continue
        jar.set_cookie(
            Cookie(
                0,
                name,
                value,
                None,
                False,
                host,
                True,
                host.startswith("."),
                path,
                True,
                bool(secure),
                expiry or None,
                not expiry,
                None,
                None,
                {},
            )
        )
    if not any(cookie.name == "sessionid" and cookie.value for cookie in jar):
        raise ValueError("No active Instagram session in the selected profile")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".instagram-", delete=False) as file:
            temporary = Path(file.name)
        os.chmod(temporary, 0o600)
        jar.save(str(temporary), ignore_discard=True)
        temporary.replace(destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return len(jar)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firefox-profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("secrets/instagram.cookies.txt"))
    args = parser.parse_args()
    try:
        count = export_profile(args.firefox_profile, args.output)
    except (OSError, sqlite3.Error, ValueError):
        parser.exit(1, "Session export failed. Check the selected profile and sign-in; close the browser if locked.\n")
    print(f"Exported {count} Instagram cookies to {args.output.resolve()}; no other domains exported.")


if __name__ == "__main__":
    main()

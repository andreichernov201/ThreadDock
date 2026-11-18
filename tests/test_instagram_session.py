import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from http.cookiejar import MozillaCookieJar
from pathlib import Path

from scripts.instagram_session import export_profile


class InstagramSessionTests(unittest.TestCase):
    def test_export_contains_only_instagram_and_drops_expired_cookies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with closing(sqlite3.connect(root / "cookies.sqlite")) as connection, connection:
                connection.execute("CREATE TABLE moz_cookies(host,path,isSecure,expiry,name,value)")
                connection.executemany(
                    "INSERT INTO moz_cookies VALUES (?,?,?,?,?,?)",
                    [
                        (".instagram.com", "/", 1, int(time.time()) + 3600, "sessionid", "fixture-session"),
                        (".youtube.com", "/", 1, int(time.time()) + 3600, "unrelated", "not-exported"),
                        (".instagram.com", "/", 1, 1, "expired", "not-exported"),
                    ],
                )
            destination = root / "export" / "session.txt"
            self.assertEqual(export_profile(root, destination), 1)
            jar = MozillaCookieJar(str(destination))
            jar.load(ignore_discard=True)
            self.assertEqual([(c.domain, c.name) for c in jar], [(".instagram.com", "sessionid")])

    def test_missing_session_does_not_overwrite_an_existing_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with closing(sqlite3.connect(root / "cookies.sqlite")) as connection, connection:
                connection.execute("CREATE TABLE moz_cookies(host,path,isSecure,expiry,name,value)")
            destination = root / "existing.txt"
            destination.write_text("keep existing", encoding="utf-8")
            with self.assertRaises(ValueError):
                export_profile(root, destination)
            self.assertEqual(destination.read_text(encoding="utf-8"), "keep existing")

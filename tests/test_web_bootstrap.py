import os
import tempfile
import unittest
from pathlib import Path

from dotenv import dotenv_values

from scripts.configure_web import archive_origin, configure


class WebBootstrapTests(unittest.TestCase):
    def test_public_ip_gets_https_without_purchasing_a_domain(self):
        self.assertEqual(archive_origin({}, "193.39.209.37"), "https://193-39-209-37.sslip.io")

    def test_custom_domain_is_preserved(self):
        self.assertEqual(
            archive_origin({"ARCHIVE_DOMAIN": "archive.example.org"}, "193.39.209.37"),
            "https://archive.example.org",
        )

    def test_private_ip_and_mismatched_origin_are_rejected(self):
        with self.assertRaises(ValueError):
            archive_origin({}, "127.0.0.1")
        with self.assertRaises(ValueError):
            archive_origin(
                {"ARCHIVE_DOMAIN": "archive.example.org", "ARCHIVE_PUBLIC_URL": "https://other.example.org"},
                "193.39.209.37",
            )

    def test_setup_is_idempotent_and_preserves_bot_token_and_vps_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "BOT_TOKEN=123:private-test-token\nDOWNLOAD_WORKERS=1\n"
                "ARCHIVE_PUBLIC_URL=\nARCHIVE_DOMAIN=\nCACHE_MAX_MB=1024\n",
                encoding="utf-8",
            )
            configure(path, "193.39.209.37")
            configure(path, "193.39.209.37")
            values = dotenv_values(path)
            self.assertEqual(values["BOT_TOKEN"], "123:private-test-token")
            self.assertEqual(values["DOWNLOAD_WORKERS"], "1")
            self.assertEqual(values["CACHE_MAX_MB"], "1024")
            self.assertEqual(path.read_text().count("ARCHIVE_PUBLIC_URL="), 1)
            self.assertTrue((path.parent / "web.enabled").exists())
            self.assertEqual((path.parent / "web-origin").read_text().strip(), values["ARCHIVE_PUBLIC_URL"])
            # Windows chmod controls the read-only bit, not POSIX permission bits.
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

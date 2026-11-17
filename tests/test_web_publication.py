import io
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from archive.config import ArchiveSettings
from scripts.check_web import check_public_app


class PublicationTests(unittest.TestCase):
    def test_domain_alone_configures_the_https_menu_url(self):
        with patch.dict(os.environ, {"ARCHIVE_DOMAIN": "archive.example.org"}, clear=True):
            self.assertEqual(ArchiveSettings.from_environment().public_url, "https://archive.example.org")

    def test_inconsistent_public_url_and_caddy_domain_fail_early(self):
        with patch.dict(
            os.environ,
            {"ARCHIVE_DOMAIN": "archive.example.org", "ARCHIVE_PUBLIC_URL": "https://wrong.example.org"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                ArchiveSettings.from_environment()

    def test_public_check_requires_assets_and_private_api(self):
        origin = "https://archive.example.org"

        def response(url, timeout):
            if url.endswith("/api/session"):
                raise HTTPError(url, 401, "Unauthorized", {}, None)
            body = {
                "/healthz": b'{"ok":true}',
                "/": b'<script src="/assets/app.js">',
                "/assets/app.js": b"/api/events",
            }[url.removeprefix(origin)]
            result = io.BytesIO(body)
            result.url = url
            return result

        with patch("scripts.check_web.urlopen", side_effect=response):
            check_public_app(origin)

    def test_container_health_without_frontend_is_not_a_successful_publication(self):
        def response(url, timeout):
            result = io.BytesIO(b'{"ok":true}')
            result.url = url
            return result

        with patch("scripts.check_web.urlopen", side_effect=response), self.assertRaises(ValueError):
            check_public_app("https://archive.example.org")

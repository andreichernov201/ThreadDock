import logging
import os
import unittest
from unittest.mock import patch

from config import Settings, load_settings
from core.errors import MediaError
from core.logging import SafeFormatter
from services.http import check_https_url


class ConfigurationTests(unittest.TestCase):
    def settings(self, environment):
        with patch("config.load_dotenv"), patch.dict(os.environ, environment, clear=True):
            return load_settings(require_token=False)

    def test_environment_and_group_ids(self):
        settings = self.settings({"DOWNLOAD_WORKERS": "3", "ALLOWED_GROUP_IDS": "-1, -2", "ENABLE_BUSINESS": "false"})
        self.assertEqual(settings.workers, 3)
        self.assertEqual(settings.groups_enabled, frozenset({-1, -2}))
        self.assertFalse(settings.business_enabled)

    def test_bad_numbers_boolean_and_ids(self):
        for environment in (
            {"DOWNLOAD_WORKERS": "0"},
            {"DOWNLOAD_TIMEOUT_SECONDS": "nan"},
            {"ENABLE_BUSINESS": "maybe"},
            {"ALLOWED_GROUP_IDS": "text"},
        ):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                self.settings(environment)

    def test_cloud_upload_limit_is_checked(self):
        with self.assertRaises(ValueError):
            Settings(token="", max_upload_bytes=100_000_000)

    def test_http_adapter_rejects_unsafe_schemes_and_credentials(self):
        for url in (
            "http://example.com/a",
            "file:///etc/passwd",
            "https://user:pass@example.com/a",
            "https://example.com:444/a",
        ):
            with self.subTest(url=url), self.assertRaises(MediaError):
                check_https_url(url)

    def test_formatter_removes_tokens_and_urls(self):
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            "file",
            1,
            "Failed https://cdn.example/video?secret=123 123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef",
            (),
            None,
        )
        result = SafeFormatter("%(message)s").format(record)
        self.assertNotIn("secret", result)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", result)

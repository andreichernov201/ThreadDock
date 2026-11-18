import unittest

from services.links import normalize_url
from services.ytdlp import build_command, source_error
from tests.support import DatabaseFixture


class AdapterTests(DatabaseFixture, unittest.TestCase):
    def test_temporary_unavailability_is_not_classified_as_permanent(self):
        error = source_error(b"Video unavailable: HTTP Error 503: temporary service failure")
        self.assertEqual(error.code, "source_temporary")
        self.assertTrue(error.retryable)

    def test_tiktok_challenge_is_explained(self):
        self.assertEqual(source_error(b"Video not available, status code 0").code, "source_blocked")

    def test_unauthorized_source_is_not_retried_as_an_extractor_bug(self):
        error = source_error(b"401 Unauthorized")
        self.assertEqual(error.code, "authentication")
        self.assertFalse(error.retryable)

    def test_queue_is_the_only_owner_of_download_retries(self):
        command = build_command(self.settings, normalize_url("https://vt.tiktok.com/abc/"), self.root, "http://guard")
        for flag in ("--retries", "--fragment-retries", "--extractor-retries"):
            self.assertEqual(command[command.index(flag) + 1], "0")

    def test_external_ffmpeg_fallback_has_local_only_inputs(self):
        command = build_command(self.settings, normalize_url("https://youtu.be/abc"), self.root, "http://guard")
        self.assertEqual(command[command.index("--downloader") + 1], "native")
        expected = "ffmpeg_i:-protocol_whitelist file,pipe,crypto"
        self.assertEqual(command[command.index("--downloader-args") + 1], expected)
        self.assertEqual(command[command.index("--postprocessor-args") + 1], expected)

    def test_upstream_errors_never_echo_credentials(self):
        error = source_error(b"ERROR: 503 https://cdn.example/video?token=private")
        self.assertTrue(error.retryable)
        self.assertNotIn("private", str(error))
        self.assertNotIn("https", str(error))

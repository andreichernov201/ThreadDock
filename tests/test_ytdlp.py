import unittest

from services.links import normalize_url
from services.ytdlp import build_command, source_error
from tests.support import DatabaseFixture


class AdapterTests(DatabaseFixture, unittest.TestCase):
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

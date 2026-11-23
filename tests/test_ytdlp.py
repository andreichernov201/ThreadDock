import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.errors import MediaError
from services.links import normalize_url
from services.processes import ProcessResult
from services.ytdlp import YtDlpAdapter, build_command, source_error
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


class SessionPreservationTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def test_filtered_live_status_has_a_specific_nonretryable_error(self):
        for marker in (b'true:false:"is_live"', b'false:true:"is_upcoming"', b'false:false:"post_live"'):
            with (
                patch("services.ytdlp.SafeProxy") as proxy,
                patch(
                    "services.ytdlp.run_process",
                    new=AsyncMock(return_value=ProcessResult(0, b"THREDDOCK_LIVE:" + marker, b"")),
                ),
            ):
                proxy.return_value.__aenter__.return_value = SimpleNamespace(url="http://guard")
                with self.assertRaises(MediaError) as caught:
                    await YtDlpAdapter(self.settings).download(normalize_url("https://youtu.be/abc"), self.root)
                self.assertEqual(caught.exception.code, "live_unsupported")
                self.assertFalse(caught.exception.retryable)

    async def test_failed_extractor_cannot_erase_the_installed_session(self):
        original = self.root / "installed-session.txt"
        original.write_bytes(b"fixture-session")
        settings = replace(self.settings, cookies_file=original)
        copies = []

        async def rejected(command, **kwargs):
            from pathlib import Path

            cookie_copy = Path(command[command.index("--cookies") + 1])
            copies.append(cookie_copy.read_bytes())
            cookie_copy.write_bytes(b"")
            return ProcessResult(1, b"", b"401 Unauthorized")

        with (
            patch("services.ytdlp.SafeProxy") as proxy,
            patch("services.ytdlp.run_process", new=AsyncMock(side_effect=rejected)),
        ):
            proxy.return_value.__aenter__.return_value = SimpleNamespace(url="http://guard")
            for name in ("first", "second"):
                work = self.root / name
                work.mkdir()
                with self.assertRaises(MediaError):
                    await YtDlpAdapter(settings).download(
                        normalize_url("https://www.instagram.com/reel/example/"), work
                    )
        self.assertEqual(copies, [b"fixture-session", b"fixture-session"])
        self.assertEqual(original.read_bytes(), b"fixture-session")

import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from core.errors import MediaError
from services.media import MediaInfo, MediaTools, parse_probe
from tests.support import DatabaseFixture


class ProbeTests(unittest.TestCase):
    def test_video_and_audio_are_detected(self):
        payload = json.dumps(
            {
                "format": {"duration": "1.5"},
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 320, "height": 240},
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
            }
        ).encode()
        info = parse_probe(payload)
        self.assertEqual((info.video_codec, info.audio_codec, info.duration), ("h264", "aac", 1.5))

    def test_invalid_and_non_finite_duration_is_rejected(self):
        for payload in (b"broken", b'{"format":{"duration":"nan"}}', b'{"format":{"duration":"inf"}}'):
            with self.subTest(payload=payload), self.assertRaises(MediaError):
                parse_probe(payload)


class SlideshowLimitTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def test_live_duration_total_is_checked_before_rendering(self):
        tools = MediaTools(replace(self.settings, max_duration=5))
        tools.probe = AsyncMock(return_value=MediaInfo(3.4, 160, 120, "h264", ""))
        tools.ffmpeg = AsyncMock()
        with self.assertRaises(MediaError) as caught:
            await tools.slideshow(
                [self.root / "one.jpg", self.root / "two.jpg"],
                None,
                self.root,
                motion={1: self.root / "motion.mp4"},
            )
        self.assertEqual(caught.exception.code, "duration_limit")
        tools.ffmpeg.assert_not_awaited()

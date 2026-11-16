import json
import unittest

from core.errors import MediaError
from services.media import parse_probe


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

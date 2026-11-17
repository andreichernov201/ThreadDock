"""Real FFmpeg/ffprobe commands against generated local fixtures, never the internet."""

import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from services.links import normalize_url
from services.media import MediaTools
from services.processes import ProcessResult
from services.ytdlp import build_command
from tests.support import DatabaseFixture


async def local_process(command, *, timeout, cwd=None, **kwargs):
    # Synchronous execution avoids Windows sandbox socketpair restrictions while
    # testing the exact production FFmpeg arguments and its real output files.
    result = subprocess.run(command, cwd=cwd, capture_output=True, timeout=timeout)
    return ProcessResult(result.returncode, result.stdout, result.stderr)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is not installed")
class FFmpegIntegrationTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.tools = MediaTools(self.settings)
        self.patch = patch("services.media.run_process", side_effect=local_process)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def fixture(self, name, color):
        path = self.root / name
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color={color}:s=160x120",
                "-frames:v",
                "1",
                "-update",
                "1",
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        return path

    async def test_one_photo_without_music(self):
        photo = self.fixture("one.jpg", "red")
        asset, has_audio = await self.tools.slideshow([photo], None, self.root)
        info = await self.tools.probe(self.root / asset.filename)
        self.assertFalse(has_audio)
        self.assertAlmostEqual(info.duration, 3, delta=0.15)
        self.assertEqual(info.video_codec, "h264")

    async def test_multiple_photos_have_three_seconds_each_with_continuous_audio(self):
        photos = [self.fixture("one.jpg", "red"), self.fixture("two.jpg", "blue")]
        music = self.root / "music.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1.2",
                "-c:a",
                "aac",
                str(music),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        asset, has_audio = await self.tools.slideshow(photos, music, self.root)
        info = await self.tools.probe(self.root / asset.filename)
        self.assertTrue(has_audio)
        self.assertAlmostEqual(info.duration, 6, delta=0.15)
        self.assertEqual(info.audio_codec, "aac")
        for second, expected in (("0.1", "red"), ("4.0", "blue")):
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    second,
                    "-i",
                    str(self.root / asset.filename),
                    "-vf",
                    "crop=10:10:540:960",
                    "-frames:v",
                    "1",
                    "-pix_fmt",
                    "rgb24",
                    "-f",
                    "rawvideo",
                    "pipe:1",
                ],
                capture_output=True,
                check=True,
                timeout=20,
            )
            red, blue = sum(result.stdout[0::3]), sum(result.stdout[2::3])
            self.assertGreater(red if expected == "red" else blue, blue if expected == "red" else red)

    async def test_single_photo_has_three_seconds_and_short_audio_is_not_extended(self):
        photo = self.fixture("one.jpg", "red")
        for duration in (8, 1.2):
            music = self.root / "music.m4a"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency=440:duration={duration}",
                    "-c:a",
                    "aac",
                    str(music),
                ],
                check=True,
                capture_output=True,
                timeout=20,
            )
            asset, has_audio = await self.tools.slideshow([photo], music, self.root)
            self.assertTrue(has_audio)
            self.assertLessEqual(asset.duration, 5.0)
            self.assertAlmostEqual(asset.duration, min(3, duration), delta=0.1)

    async def test_all_slides_follow_full_live_duration_and_preserve_motion(self):
        photos = [self.fixture("one.jpg", "red"), self.fixture("two.jpg", "blue")]
        clip = self.root / "motion.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x120:rate=15:duration=3.4",
                "-c:v",
                "libx264",
                str(clip),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        music = self.root / "music.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:a",
                "aac",
                str(music),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        asset, has_audio = await self.tools.slideshow(photos, music, self.root, motion={1: clip})
        self.assertAlmostEqual(asset.duration, 6.8, delta=0.1)
        self.assertTrue(has_audio)
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                "3.4",
                "-i",
                str(asset.path(self.root)),
                "-vf",
                "fps=5,scale=64:64",
                "-an",
                "-f",
                "framemd5",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=20,
        )
        hashes = {line.split(",")[-1] for line in result.stdout.decode().splitlines() if not line.startswith("#")}
        self.assertGreater(len(hashes), 3)

    async def test_multiple_live_photos_use_longest_with_two_second_minimum(self):
        photos = [self.fixture("one.jpg", "red"), self.fixture("two.jpg", "blue")]
        clips = []
        for index, duration in enumerate((1, 2.4)):
            clip = self.root / f"motion-{index}.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"testsrc2=size=160x120:rate=15:duration={duration}",
                    "-c:v",
                    "libx264",
                    str(clip),
                ],
                check=True,
                capture_output=True,
                timeout=20,
            )
            clips.append(clip)
        asset, _ = await self.tools.slideshow(photos, None, self.root, motion={0: clips[0], 1: clips[1]})
        self.assertAlmostEqual(asset.duration, 4.8, delta=0.1)
        asset, _ = await self.tools.slideshow(photos, None, self.root, motion={1: clips[0]})
        self.assertAlmostEqual(asset.duration, 4, delta=0.1)

    async def test_single_live_photo_is_not_cut_to_short_music(self):
        photo = self.fixture("one.jpg", "red")
        clip, music = self.root / "motion.mp4", self.root / "music.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x120:rate=15:duration=2.4",
                "-c:v",
                "libx264",
                str(clip),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:a",
                "aac",
                str(music),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        asset, has_audio = await self.tools.slideshow([photo], music, self.root, motion={0: clip})
        self.assertAlmostEqual(asset.duration, 2.4, delta=0.1)
        self.assertTrue(has_audio)


@unittest.skipUnless(shutil.which("yt-dlp"), "yt-dlp is not installed")
class ExtractorOptionsTests(DatabaseFixture, unittest.TestCase):
    def test_real_cli_selects_separate_https_streams_without_network(self):
        metadata = self.root / "fixture.json"
        metadata.write_text(
            json.dumps(
                {
                    "id": "fixture",
                    "title": "Fixture",
                    "extractor": "test",
                    "extractor_key": "Test",
                    "formats": [
                        {
                            "format_id": "combined",
                            "url": "https://cdn.example/720.mp4",
                            "protocol": "https",
                            "ext": "mp4",
                            "height": 720,
                            "vcodec": "avc1",
                            "acodec": "mp4a",
                        },
                        {
                            "format_id": "video",
                            "url": "https://cdn.example/1080.mp4",
                            "protocol": "https",
                            "ext": "mp4",
                            "height": 1080,
                            "vcodec": "avc1",
                            "acodec": "none",
                        },
                        {
                            "format_id": "audio",
                            "url": "https://cdn.example/audio.m4a",
                            "protocol": "https",
                            "ext": "m4a",
                            "vcodec": "none",
                            "acodec": "mp4a",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        command = build_command(self.settings, normalize_url("https://youtu.be/abc"), self.root, "http://127.0.0.1:1")
        command = command[: command.index("--")] + [
            "--simulate",
            "--dump-single-json",
            "--load-info-json",
            str(metadata),
        ]
        result = subprocess.run(command, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        selected = json.loads(result.stdout)
        self.assertEqual([item["format_id"] for item in selected["requested_formats"]], ["video", "audio"])

import asyncio
import unittest
from unittest.mock import AsyncMock

from core.models import Asset
from services.pipeline import KeyedLocks, MediaPipeline
from tests.support import DatabaseFixture


class PipelineTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.pipeline = MediaPipeline(self.settings, self.database, None)
        self.pipeline.video.download = AsyncMock(side_effect=self.download)
        self.pipeline.tools.compatible_video = AsyncMock(side_effect=self.convert)
        self.pipeline.tools.extract_audio = AsyncMock(return_value=None)

    async def download(self, link, directory):
        path = directory / "source.mp4"
        path.write_bytes(b"source")
        (directory / "source.info.json").write_text('{"secret":"signed-url"}')
        return path, "Example"

    async def convert(self, source, directory):
        (directory / "video.mp4").write_bytes(b"processed")
        return Asset("video.mp4", "video", 1, 320, 240), True

    async def test_download_publishes_only_result_assets(self):
        bundle = await self.pipeline.fetch("https://youtu.be/abc")
        self.assertEqual([path.name for path in bundle.directory.iterdir()], ["video.mp4"])
        self.assertEqual(list(self.pipeline.work.iterdir()), [])

    async def test_repeated_link_reuses_cache(self):
        first = await self.pipeline.fetch("https://youtu.be/abc?si=one")
        second = await self.pipeline.fetch("https://youtu.be/abc?si=two")
        self.assertEqual(first, second)
        self.pipeline.video.download.assert_awaited_once()

    async def test_concurrent_identical_links_download_once(self):
        first, second = await asyncio.gather(
            self.pipeline.fetch("https://youtu.be/abc"), self.pipeline.fetch("https://youtu.be/abc")
        )
        self.assertEqual(first, second)
        self.pipeline.video.download.assert_awaited_once()
        self.assertEqual(self.pipeline.locks._entries, {})

    async def test_failed_download_cleans_workspace(self):
        self.pipeline.video.download.side_effect = OSError("private-url-must-not-be-shown")
        with self.assertRaisesRegex(Exception, "сохранить") as captured:
            await self.pipeline.fetch("https://youtu.be/abc")
        self.assertNotIn("private-url", str(captured.exception))
        self.assertEqual(list(self.pipeline.work.iterdir()), [])

    async def test_expired_cache_is_removed(self):
        bundle = await self.pipeline.fetch("https://youtu.be/abc")
        self.database.connection.execute("UPDATE cache SET expires_at=0")
        self.database.connection.commit()
        self.pipeline.maintain()
        self.assertFalse(bundle.directory.exists())

    async def test_active_job_pins_cache_during_maintenance(self):
        bundle = await self.pipeline.fetch("https://youtu.be/abc")
        self.job()
        self.database.connection.execute("UPDATE cache SET expires_at=0")
        self.database.connection.commit()
        self.pipeline.maintain()
        self.assertTrue(bundle.directory.exists())

    async def test_orphan_after_interrupted_publish_is_cleaned(self):
        orphan = self.pipeline.cache / ("a" * 64)
        orphan.mkdir()
        (orphan / "video.mp4").write_bytes(b"orphan")
        self.pipeline.maintain()
        self.assertFalse(orphan.exists())

    async def test_oversized_optional_audio_does_not_discard_video(self):
        from core.errors import MediaError

        self.pipeline.tools.extract_audio.side_effect = MediaError("Too large", code="file_too_large")
        bundle = await self.pipeline.fetch("https://youtu.be/abc")
        self.assertTrue((bundle.directory / bundle.video.filename).is_file())
        self.assertIsNone(bundle.audio)


class LockTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_waiter_does_not_leak_lock(self):
        locks = KeyedLocks()
        async with locks.hold("key"):

            async def waiting():
                async with locks.hold("key"):
                    pass

            task = asyncio.create_task(waiting())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(locks._entries, {})

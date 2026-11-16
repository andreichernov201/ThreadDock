import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendVideo

from services.delivery import TelegramDelivery
from services.jobs import JobQueue
from storage.database import Database
from tests.support import DatabaseFixture
from tests.test_delivery import sent


class DeliveryCacheTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.bot = SimpleNamespace(id=123, send_video=AsyncMock(return_value=sent()))
        self.delivery = TelegramDelivery(self.bot, self.database, self.settings)
        self.pipeline = SimpleNamespace(fetch=AsyncMock())

    async def saved_video(self):
        job = self.job()
        bundle = self.bundle()
        self.database.put_cache(job.cache_key, bundle, 60)
        await self.delivery.deliver(job, bundle)
        self.database.status(job.id, "done")
        (bundle.directory / "video.mp4").unlink()
        self.database.delete_cache(job.cache_key)
        return bundle

    async def test_disk_eviction_still_sends_telegram_file_without_downloading(self):
        await self.saved_video()
        job = self.job(message_id=2)
        queue = JobQueue(self.settings, self.database, self.pipeline, self.delivery)
        await queue._process(job)
        self.pipeline.fetch.assert_not_awaited()
        self.assertEqual(self.bot.send_video.call_args.kwargs["video"], "remote-1")
        self.assertEqual(self.database.get_job(job.id)["status"], "done")

    async def test_stale_remote_reference_refetches_once(self):
        await self.saved_video()
        job = self.job(message_id=2)
        self.bot.send_video.side_effect = [
            TelegramBadRequest(method=SendVideo(chat_id=10, video="old"), message="wrong file identifier"),
            sent(2),
        ]
        self.pipeline.fetch.side_effect = lambda url, **kwargs: self.bundle()
        queue = JobQueue(self.settings, self.database, self.pipeline, self.delivery)
        await queue._process(job)
        self.pipeline.fetch.assert_awaited_once_with(job.url, force=True)
        self.assertEqual(self.database.get_job(job.id)["status"], "done")

    async def test_cached_video_does_not_promise_unavailable_audio(self):
        await self.saved_video()
        job = self.job(mode="audio", message_id=2)
        self.assertIsNone(self.delivery.cached(job))

    async def test_plain_video_has_no_redundant_source_button(self):
        self.assertIsNone(self.delivery.keyboard(self.job(), self.bundle()))

    async def test_existing_sent_cache_is_migrated_before_disk_cleanup(self):
        job, bundle = self.job(), self.bundle()
        self.database.put_cache(job.cache_key, bundle, 0)
        self.database.save_file_id(123, job.cache_key, "video.mp4", "video", "old-remote")
        self.database.close()
        self.database = Database(self.root / "test.sqlite3", self.root)
        self.addCleanup(self.database.close)
        self.database.delete_cache(job.cache_key)
        self.assertIsNotNone(self.database.get_delivery_cache(123, job.cache_key, "video"))

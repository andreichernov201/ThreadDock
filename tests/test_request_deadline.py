import asyncio
import time
import unittest
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from services.jobs import JobQueue
from tests.support import DatabaseFixture


class RequestDeadlineTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.pipeline = SimpleNamespace(using=lambda b: nullcontext(), fetch=AsyncMock(return_value=self.bundle()))
        self.delivery = SimpleNamespace(
            permitted=lambda d: True,
            cached=lambda j: None,
            deliver=AsyncMock(),
            text=AsyncMock(),
        )
        self.queue = JobQueue(self.settings, self.database, self.pipeline, self.delivery)

    async def test_queue_wait_expires_even_when_telegram_cache_is_available(self):
        job = self.job()
        self.database.connection.execute("UPDATE jobs SET requested_at=? WHERE id=?", (time.time() - 61, job.id))
        self.database.connection.commit()
        self.delivery.cached = lambda j: self.bundle()
        await self.queue._process(job)
        self.assertEqual(self.database.get_job(job.id)["error_code"], "request_expired")
        self.pipeline.fetch.assert_not_awaited()
        self.delivery.deliver.assert_not_awaited()
        self.assertIsNone(self.database.claim())

    async def test_timeout_cancels_processing_and_never_sends_a_late_result(self):
        cancelled = asyncio.Event()

        async def slow(*args):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        self.pipeline.fetch.side_effect = slow
        self.queue.settings = replace(self.settings, request_timeout=0.15)
        job = self.job()
        await self.queue._process(job)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.database.get_job(job.id)["status"], "failed")
        self.delivery.deliver.assert_not_awaited()
        self.assertIsNone(self.database.claim())

    async def test_timeout_during_upload_is_uncertain_and_never_auto_replayed(self):
        async def upload(job, bundle):
            self.database.status(job.id, "sending")
            await asyncio.sleep(10)

        self.delivery.deliver.side_effect = upload
        self.queue.settings = replace(self.settings, request_timeout=0.15)
        job = self.job()
        await self.queue._process(job)
        row = self.database.get_job(job.id)
        self.assertEqual(row["status"], "uncertain")
        self.assertFalse(row["retryable"])
        self.assertIsNone(self.database.claim())

    async def test_accepted_media_stays_done_if_optional_keyboard_runs_out_of_time(self):
        async def upload(job, bundle):
            self.database.delivered_piece(job.id, 0, [1])
            await asyncio.sleep(10)

        self.delivery.deliver.side_effect = upload
        self.queue.settings = replace(self.settings, request_timeout=0.15)
        job = self.job()
        await self.queue._process(job)
        self.assertEqual(self.database.get_job(job.id)["status"], "done")
        self.delivery.text.assert_not_awaited()

    async def test_successful_processing_does_not_extend_an_expired_delivery_window(self):
        async def fetch(*args):
            self.database.connection.execute("UPDATE jobs SET requested_at=? WHERE id=?", (time.time() - 61, job.id))
            self.database.connection.commit()
            # This models a source client that returns after consuming its budget.
            return self.bundle()

        from services.delivery import TelegramDelivery

        bot = SimpleNamespace(id=123, send_video=AsyncMock(), send_message=AsyncMock())
        self.queue.delivery = TelegramDelivery(bot, self.database, self.settings)
        self.pipeline.fetch.side_effect = fetch
        job = self.job()
        await self.queue._process(job)
        bot.send_video.assert_not_awaited()
        self.assertEqual(self.database.get_job(job.id)["error_code"], "request_expired")

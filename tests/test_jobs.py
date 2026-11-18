import unittest
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendVideo

from core.errors import MediaError, MissingAsset
from services.jobs import JobQueue
from tests.support import DatabaseFixture


class JobTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.pipeline = SimpleNamespace(using=lambda bundle: nullcontext(), fetch=AsyncMock(return_value=self.bundle()))
        self.delivery = SimpleNamespace(
            permitted=lambda target: True, cached=lambda job: None, deliver=AsyncMock(), text=AsyncMock()
        )
        self.queue = JobQueue(self.settings, self.database, self.pipeline, self.delivery)

    async def test_success_is_terminal(self):
        job = self.job()
        await self.queue._process(job)
        self.assertEqual(self.database.get_job(job.id)["status"], "done")

    async def test_transient_source_failure_is_requeued(self):
        job = self.job()
        self.pipeline.fetch.side_effect = MediaError("Temporary", code="network", retryable=True)
        await self.queue._process(job)
        row = self.database.get_job(job.id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["error_code"], "network")

    async def test_permanent_failure_notifies_once(self):
        job = self.job()
        self.pipeline.fetch.side_effect = MediaError("Unavailable", code="unavailable")
        await self.queue._process(job)
        self.assertEqual(self.database.get_job(job.id)["status"], "failed")
        self.delivery.text.assert_awaited_once()
        self.assertIsNone(self.delivery.text.call_args.args[2])

    async def test_exhausted_transient_failure_offers_scoped_retry(self):
        from dataclasses import replace

        job = self.job()
        job = replace(job, attempts=self.settings.retries + 1)
        self.pipeline.fetch.side_effect = MediaError("Temporary", code="source_temporary", retryable=True)
        await self.queue._process(job)
        markup = self.delivery.text.call_args.args[2]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, f"r:{job.id}:{job.attempts}")
        self.assertTrue(self.database.get_job(job.id)["retryable"])

    async def test_network_failure_during_delivery_is_not_replayed(self):
        job = self.job()
        self.delivery.deliver.side_effect = TelegramNetworkError(
            method=SendVideo(chat_id=10, video="file"), message="lost response"
        )
        await self.queue._process(job)
        self.assertEqual(self.database.get_job(job.id)["status"], "uncertain")
        self.assertIsNone(self.database.claim())
        self.assertIsNone(self.delivery.text.call_args.args[2])

    async def test_failed_connection_before_request_is_safe_to_retry(self):
        from aiohttp import ClientConnectorError

        job = self.job()
        error = TelegramNetworkError(method=SendVideo(chat_id=10, video="file"), message="connect failed")
        error.__cause__ = ClientConnectorError(
            SimpleNamespace(ssl=True, host="example.org", port=443), OSError("unreachable")
        )
        self.delivery.deliver.side_effect = error
        await self.queue._process(job)
        row = self.database.get_job(job.id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["error_code"], "telegram_connect")
        self.delivery.text.assert_not_awaited()

    async def test_telegram_rate_limit_schedules_instead_of_failing(self):
        job = self.job()
        self.delivery.deliver.side_effect = TelegramRetryAfter(
            method=SendVideo(chat_id=10, video="file"), message="rate", retry_after=3
        )
        await self.queue._process(job)
        self.assertEqual(self.database.get_job(job.id)["status"], "queued")
        self.assertIsNone(self.database.claim())

    async def test_missing_cache_refetches_and_keeps_progress(self):
        job = self.job(mode="photos")
        self.database.delivered_piece(job.id, 0, [11])
        self.delivery.deliver.side_effect = [MissingAsset(), None]
        await self.queue._process(job)
        self.assertEqual(self.pipeline.fetch.call_args.kwargs, {"force": True})
        self.assertEqual(self.delivery.deliver.call_args.args[0].progress, 1)

    async def test_revoked_permission_prevents_download(self):
        job = self.job()
        self.delivery.permitted = lambda target: False
        await self.queue._process(job)
        self.pipeline.fetch.assert_not_awaited()

    async def test_permanent_source_error_has_no_retry_button(self):
        job = self.job()
        self.pipeline.fetch.side_effect = MediaError("Unavailable", code="unavailable")
        await self.queue._process(job)
        self.assertIsNone(self.delivery.text.call_args.args[2])

    async def test_exhausted_transient_error_keeps_one_working_retry_button(self):
        job = self.job(mode="photos")
        self.queue.settings = replace(self.settings, retries=0)
        self.pipeline.fetch.side_effect = MediaError("Temporary", code="network", retryable=True)
        await self.queue._process(job)
        keyboard = self.delivery.text.call_args.args[2]
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, f"r:{job.id}:{job.attempts}")

import asyncio
import unittest
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.errors import MediaError
from core.models import Destination
from services.jobs import JobQueue
from tests.support import DatabaseFixture


class OrderedQueueTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.pipeline = SimpleNamespace(using=lambda b: nullcontext(), fetch=AsyncMock(return_value=self.bundle()))
        self.delivery = SimpleNamespace(
            permitted=lambda d: True, cached=lambda j: None, deliver=AsyncMock(), text=AsyncMock()
        )
        self.queue = JobQueue(self.settings, self.database, self.pipeline, self.delivery)

    def enqueue_pair(self):
        self.database.enqueue(
            [("one", "https://youtu.be/one", "one"), ("two", "https://youtu.be/two", "two")],
            Destination(10, 1, 20),
            "video",
            100,
        )
        return self.database.claim(parallel=True), self.database.claim(parallel=True)

    async def test_parallel_fetch_finishes_out_of_order_but_delivery_stays_ordered(self):
        first, second = self.enqueue_pair()
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(url):
            if url.endswith("one"):
                entered.set()
                await release.wait()
            return self.bundle()

        self.pipeline.fetch.side_effect = fetch
        task = asyncio.create_task(self.queue._process(first, prepare_only=True))
        await asyncio.wait_for(entered.wait(), 1)
        await self.queue._process(second, prepare_only=True)
        self.assertEqual(self.database.get_job(second.id)["status"], "ready")
        self.assertIsNone(self.database.claim_ready())
        release.set()
        await task
        await self.queue._deliver_ready(self.database.claim_ready())
        await self.queue._deliver_ready(self.database.claim_ready())
        self.assertEqual([call.args[0].id for call in self.delivery.deliver.await_args_list], [first.id, second.id])

    async def test_failure_notice_waits_its_turn(self):
        first, second = self.enqueue_pair()
        self.pipeline.fetch.side_effect = MediaError("gone", code="unavailable")
        await self.queue._process(second, prepare_only=True)
        self.delivery.text.assert_not_awaited()
        self.assertIsNone(self.database.claim_ready())
        self.pipeline.fetch.side_effect = None
        await self.queue._process(first, prepare_only=True)
        await self.queue._deliver_ready(self.database.claim_ready())
        self.delivery.text.assert_not_awaited()
        await self.queue._deliver_ready(self.database.claim_ready())
        self.delivery.text.assert_awaited_once()
        self.assertEqual(self.database.get_job(second.id)["status"], "failed")

    async def test_ready_bundle_survives_restart(self):
        job = self.job()
        bundle = self.bundle()
        self.database.put_cache(job.cache_key, bundle, 3600)
        await self.queue._process(job, prepare_only=True)
        self.queue.prepared.clear()
        self.database.recover()
        await self.queue._deliver_ready(self.database.claim_ready())
        self.assertEqual(self.database.get_job(job.id)["status"], "done")
        self.assertEqual(self.pipeline.fetch.await_count, 1)

    def test_claimed_delivery_and_its_error_survive_restart(self):
        job = self.job()
        self.database.status(job.id, "ready", "unavailable")
        self.database.claim_ready()
        self.database.recover()
        again = self.database.claim_ready()
        self.assertEqual(again.id, job.id)
        self.assertEqual(self.database.get_job(job.id)["error_code"], "unavailable")

    async def test_timeout_during_preparation_is_reported_in_delivery_order(self):
        job = self.job()
        self.queue.settings = replace(self.settings, request_timeout=0.02)
        self.pipeline.fetch.side_effect = lambda *a: None

        async def slow(*args):
            await asyncio.sleep(1)

        self.pipeline.fetch.side_effect = slow
        await self.queue._process(job, prepare_only=True)
        row = self.database.get_job(job.id)
        self.assertEqual(row["status"], "ready")
        self.assertTrue(row["retryable"])
        self.delivery.text.assert_not_awaited()
        await self.queue._deliver_ready(self.database.claim_ready())
        self.delivery.text.assert_awaited_once()
        self.delivery.deliver.assert_not_awaited()

    def test_queue_and_ready_wait_do_not_consume_budget_but_retries_do(self):
        with patch("storage.database.time.time", return_value=1000):
            self.database.enqueue([("one", "https://youtu.be/one", "one")], Destination(10, 1, 20), "video", 100)
        with patch("storage.database.time.time", return_value=2000):
            job = self.database.claim(parallel=True)
            self.assertEqual(self.database.request_remaining(job.id, 60), 60)
        with patch("storage.database.time.time", return_value=2020):
            self.database.status(job.id, "ready")
        with patch("storage.database.time.time", return_value=3000):
            self.assertEqual(self.database.request_remaining(job.id, 60), 40)
            self.database.claim_ready()
        with patch("storage.database.time.time", return_value=3010):
            self.database.status(job.id, "queued", "rate", delay=2)
        with patch("storage.database.time.time", return_value=3012):
            self.database.claim(parallel=True)
            self.assertEqual(self.database.request_remaining(job.id, 60), 28)

    def test_recovery_preserves_spent_time_without_charging_downtime(self):
        job = self.job()
        with patch("storage.database.time.time", return_value=1000):
            self.database.connection.execute("UPDATE jobs SET active_since=990,active_elapsed=12 WHERE id=?", (job.id,))
            self.database.set_state("heartbeat", "1000")
        with patch("storage.database.time.time", return_value=5000):
            self.database.recover()
            self.database.claim(parallel=True)
            self.assertEqual(self.database.request_remaining(job.id, 60), 28)

    def test_explicit_new_message_after_uncertain_is_accepted_once(self):
        job = self.job()
        self.database.status(job.id, "uncertain", "delivery_uncertain")
        self.assertFalse(self.database.retry_job(job.id, job.attempts, 100))
        request = [("new-message", job.url, job.cache_key)]
        destination = replace(job.destination, message_id=2)
        self.assertEqual(self.database.enqueue(request, destination, "video", 100), 1)
        self.assertEqual(self.database.enqueue(request, destination, "video", 100), 0)
        self.assertEqual(self.database.claim().destination.message_id, 2)

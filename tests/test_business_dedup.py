import asyncio
import time
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendVideo

from core.errors import MediaError, MissingAsset, QueueFull
from handlers.preferences import change_preference, panel
from services.application import Application
from storage.database import Database
from tests.support import DatabaseFixture
from tests.test_handlers import incoming


class BusinessDedupTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.bot = SimpleNamespace(
            id=123,
            send_message=AsyncMock(),
            send_video=AsyncMock(
                return_value=SimpleNamespace(message_id=2001, video=SimpleNamespace(file_id="video-id"))
            ),
            get_chat_member=AsyncMock(),
        )
        self.app = Application(self.settings, self.database, self.bot, None)
        self.queue = self.app.queue
        self.queue.pipeline = SimpleNamespace(
            fetch=AsyncMock(return_value=self.bundle()), using=lambda b: nullcontext()
        )
        self.database.save_connection("a", 99, True, True)
        self.database.save_connection("b", 20, True, True)
        self.database.touch_business_chat("a", 20, time.time())
        self.database.touch_business_chat("b", 99, time.time())
        self.date = incoming().date

    async def submit(self, side, *, message_id=None, text="https://youtu.be/abc", sender=20, date=None):
        message = incoming(text, chat_id=20 if side == "a" else 99, sender=sender, business=side).model_copy(
            update={"message_id": message_id or (1 if side == "a" else 901), "date": date or self.date}
        )
        destination = await self.app.destination(message)
        self.assertIsNotNone(destination)
        return self.app.submit(destination, text)[0]

    def claim_pair(self):
        return self.database.claim(parallel=True), self.database.claim(parallel=True)

    async def prepare(self, job):
        await self.queue._process(job, prepare_only=True)

    async def deliver_next(self):
        job = self.database.claim_ready()
        self.assertIsNotNone(job)
        await self.queue._deliver_ready(job)
        return job

    async def test_faster_preparation_wins_even_when_admitted_second(self):
        self.database.set_preference("user", 99, "photo_seconds", 10)
        await self.submit("a")
        await self.submit("b")
        first, fast = self.claim_pair()
        self.assertNotEqual(first.cache_key, fast.cache_key)
        await self.prepare(fast)
        self.assertEqual((await self.deliver_next()).id, fast.id)
        await self.prepare(first)
        self.bot.send_video.assert_awaited_once()
        self.assertEqual(self.bot.send_video.call_args.kwargs["business_connection_id"], "b")
        self.assertEqual(self.database.get_job(first.id)["status"], "cancelled")
        self.assertIsNone(self.database.claim_ready())

    async def test_late_slow_worker_does_not_resurrect_or_notify_a_cancelled_request(self):
        await self.submit("a")
        await self.submit("b")
        slow, fast = self.claim_pair()
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def fetch(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
            return self.bundle()

        self.queue.pipeline.fetch.side_effect = fetch
        task = asyncio.create_task(self.prepare(slow))
        await started.wait()
        await self.prepare(fast)
        await self.deliver_next()
        release.set()
        await task
        self.assertEqual(self.database.get_job(slow.id)["status"], "cancelled")
        self.assertNotIn(slow.id, self.queue.prepared)
        self.bot.send_video.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()

    async def test_losing_download_is_cancelled_without_stopping_its_worker(self):
        from datetime import timedelta

        await self.submit("a")
        await self.submit("b")
        started, cancelled = asyncio.Event(), asyncio.Event()
        calls = 0

        async def fetch(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return self.bundle()

        self.queue.pipeline.fetch.side_effect = fetch
        workers = [asyncio.create_task(self.queue._worker()) for _ in range(2)]

        async def ready_count(count):
            while (
                self.database.connection.execute("SELECT count(*) FROM jobs WHERE status='ready'").fetchone()[0] < count
            ):
                await asyncio.sleep(0.001)

        try:
            await started.wait()
            await asyncio.wait_for(ready_count(1), 1)
            await self.deliver_next()
            await asyncio.wait_for(cancelled.wait(), 1)
            self.assertFalse(any(task.done() for task in workers))
            await self.submit("a", message_id=2, date=self.date + timedelta(seconds=1))
            await asyncio.wait_for(ready_count(1), 1)
            self.assertFalse(any(task.done() for task in workers))
        finally:
            self.queue.stopping.set()
            await asyncio.wait_for(asyncio.gather(*workers), 2)

    async def test_failed_preparation_waits_for_the_other_route_and_does_not_send_an_error(self):
        await self.submit("a")
        await self.submit("b")
        first, second = self.claim_pair()
        self.queue.pipeline.fetch.side_effect = [MediaError("Unavailable", code="unavailable"), self.bundle()]
        await self.prepare(first)
        self.assertIsNone(self.database.claim_ready())
        await self.prepare(second)
        self.assertEqual((await self.deliver_next()).id, second.id)
        self.bot.send_message.assert_not_awaited()
        self.bot.send_video.assert_awaited_once()

    async def test_known_upload_rejection_allows_the_other_route(self):
        await self.submit("a")
        await self.submit("b")
        first, second = self.claim_pair()
        await self.prepare(first)
        await self.prepare(second)
        good = self.bot.send_video.return_value
        self.bot.send_video.side_effect = [
            TelegramBadRequest(method=SendVideo(chat_id=20, video="id"), message="rejected"),
            good,
        ]
        await self.deliver_next()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual((await self.deliver_next()).id, second.id)
        self.assertEqual(self.database.get_job(second.id)["status"], "done")
        self.assertEqual(self.database.connection.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)

    async def test_explicit_rate_limit_releases_the_route_before_any_confirmed_piece(self):
        await self.submit("a")
        await self.submit("b")
        first, second = self.claim_pair()
        await self.prepare(first)
        await self.prepare(second)
        good = self.bot.send_video.return_value
        self.bot.send_video.side_effect = [
            TelegramRetryAfter(method=SendVideo(chat_id=20, video="id"), message="wait", retry_after=1),
            good,
        ]
        await self.deliver_next()
        self.assertEqual((await self.deliver_next()).id, second.id)
        self.assertEqual(self.database.get_job(first.id)["status"], "cancelled")

    async def test_missing_local_cache_does_not_hold_the_second_ready_route(self):
        await self.submit("a")
        await self.submit("b")
        first, second = self.claim_pair()
        await self.prepare(first)
        await self.prepare(second)
        self.queue.delivery.deliver = AsyncMock(side_effect=[MissingAsset(), None])
        await self.deliver_next()
        self.assertEqual(self.database.get_job(first.id)["status"], "queued")
        self.assertEqual((await self.deliver_next()).id, second.id)
        self.assertEqual(self.database.get_job(second.id)["status"], "done")

    async def test_uncertain_upload_blocks_the_other_route_across_restart(self):
        await self.submit("a")
        await self.submit("b")
        first, second = self.claim_pair()
        await self.prepare(first)
        await self.prepare(second)
        self.bot.send_video.side_effect = TelegramNetworkError(
            method=SendVideo(chat_id=20, video="id"), message="unknown"
        )
        await self.deliver_next()
        self.assertEqual(self.database.get_job(first.id)["status"], "uncertain")
        self.database.recover()
        self.assertEqual(self.database.get_job(second.id)["status"], "cancelled")
        self.assertIsNone(self.database.claim_ready())
        self.assertIsNone(self.database.claim(parallel=True))
        self.bot.send_video.assert_awaited_once()

    async def test_restart_between_claim_and_upload_keeps_single_owner(self):
        await self.submit("a")
        await self.submit("b")
        first, second = self.claim_pair()
        await self.prepare(first)
        await self.prepare(second)
        chosen = self.database.claim_ready()
        self.database.recover()
        resumed = self.database.claim_ready()
        self.assertEqual(resumed.id, chosen.id)
        self.assertIsNone(self.database.claim_ready())
        await self.queue._deliver_ready(resumed)
        self.bot.send_video.assert_awaited_once()

    async def test_restart_during_upload_blocks_both_automatic_replays(self):
        await self.submit("a")
        await self.submit("b")
        first, second = self.claim_pair()
        await self.prepare(first)
        await self.prepare(second)
        chosen = self.database.claim_ready()
        self.database.status(chosen.id, "sending")
        self.database.recover()
        self.assertEqual(self.database.get_job(chosen.id)["status"], "uncertain")
        self.assertIsNone(self.database.claim_ready())

    async def test_late_mirror_after_delivery_is_suppressed_and_persists(self):
        await self.submit("a")
        first = self.database.claim(parallel=True)
        await self.prepare(first)
        await self.deliver_next()
        self.database.close()
        self.database = Database(self.root / "test.sqlite3", self.root)
        self.addCleanup(self.database.close)
        self.app.database = self.database
        self.assertEqual(await self.submit("b"), 0)
        self.assertEqual(self.database.connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    async def test_multiple_links_keep_order_across_both_owners(self):
        text = "https://youtu.be/first https://youtu.be/second"
        await self.submit("a", text=text)
        await self.submit("b", text=text)
        jobs = [self.database.claim(parallel=True) for _ in range(4)]
        await self.prepare(jobs[3])
        await self.prepare(jobs[1])
        self.assertIsNone(self.database.claim_ready())
        await self.prepare(jobs[2])
        self.assertIn("first", (await self.deliver_next()).url)
        self.assertEqual((await self.deliver_next()).id, jobs[3].id)
        self.assertEqual(self.bot.send_video.await_count, 2)
        self.assertIsNone(self.database.claim_ready())

    async def test_two_identical_messages_in_same_second_are_two_requests(self):
        for side, ids in (("a", (1, 2)), ("b", (901, 902))):
            for message_id in ids:
                self.assertEqual(await self.submit(side, message_id=message_id), 1)
        rows = self.database.connection.execute(
            "SELECT business_request_id,count(*) FROM jobs GROUP BY business_request_id"
        ).fetchall()
        self.assertEqual([row[1] for row in rows], [2, 2])
        for _ in range(4):
            job = self.database.claim(parallel=True)
            if job:
                await self.prepare(job)
        await self.deliver_next()
        await self.deliver_next()
        self.assertEqual(self.bot.send_video.await_count, 2)

    async def test_repeated_update_is_idempotent_and_unrelated_conversations_are_separate(self):
        self.assertEqual(await self.submit("a"), 1)
        self.assertEqual(await self.submit("a"), 0)
        self.database.save_connection("c", 30, True, True)
        msg = incoming(chat_id=99, sender=99, business="c").model_copy(update={"date": self.date})
        self.assertEqual(self.app.submit(await self.app.destination(msg), msg.text)[0], 1)
        rows = self.database.connection.execute("SELECT count(*) FROM business_events").fetchone()[0]
        self.assertEqual(rows, 2)

    async def test_two_failures_send_only_one_notice(self):
        await self.submit("a")
        await self.submit("b")
        self.queue.pipeline.fetch.side_effect = MediaError("Unavailable", code="unavailable")
        for job in self.claim_pair():
            await self.prepare(job)
        await self.deliver_next()
        await self.deliver_next()
        self.bot.send_message.assert_awaited_once()

    async def test_first_confirmed_album_piece_keeps_winner_if_later_piece_fails(self):
        await self.submit("a")
        await self.submit("b")
        first, other = self.claim_pair()
        await self.prepare(first)
        await self.prepare(other)
        chosen = self.database.claim_ready()
        self.database.status(chosen.id, "sending")
        self.database.delivered_piece(chosen.id, 0, [2001, 2002])
        self.database.status(chosen.id, "failed", "telegram_rejected")
        self.assertEqual(self.database.get_job(other.id)["status"], "cancelled")
        self.assertIsNone(self.database.claim_ready())

    async def test_ready_wait_does_not_consume_request_budget(self):
        await self.submit("a")
        await self.submit("b")
        first, other = self.claim_pair()
        await self.prepare(first)
        await self.prepare(other)
        before = self.database.request_remaining(other.id, 60)
        with patch("storage.database.time.time", return_value=time.time() + 600):
            self.assertAlmostEqual(self.database.request_remaining(other.id, 60), before)

    async def test_queue_capacity_rolls_back_the_event_and_all_its_links(self):
        from dataclasses import replace

        self.app.settings = replace(self.settings, queue_capacity=1)
        with self.assertRaises(QueueFull):
            await self.submit("a", text="https://youtu.be/first https://youtu.be/second")
        for table in ("jobs", "business_events", "business_event_routes", "business_requests"):
            self.assertEqual(self.database.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)


class BusinessMigrationTests(DatabaseFixture, unittest.TestCase):
    def test_old_schema_and_personal_rules_migrate_without_changing_group_or_media_preferences(self):
        job = self.job()
        self.database.set_preference("user", 99, "photo_seconds", 10)
        self.database.set_preference("group", -1, "senders", "admins")
        with self.database.connection:
            self.database.connection.execute("UPDATE download_preferences SET senders='self' WHERE scope='user'")
            self.database.connection.execute("DROP INDEX jobs_business_request")
            self.database.connection.execute("ALTER TABLE jobs DROP COLUMN business_request_id")
        self.database.close()
        self.database = Database(self.root / "test.sqlite3", self.root)
        self.addCleanup(self.database.close)
        self.assertEqual(self.database.get_job(job.id)["business_request_id"], 0)
        self.assertEqual(self.database.preferences("user", 99)["photo_seconds"], 10)
        self.assertNotIn("senders", self.database.preferences("user", 99))
        self.assertEqual(self.database.preferences("group", -1)["senders"], "admins")


class PersonalPanelTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def test_personal_panel_and_old_sender_buttons_use_automatic_handling(self):
        bot = SimpleNamespace(id=123)
        app = Application(self.settings, self.database, bot, None)
        text, keyboard = panel("user", 10, app)
        self.assertNotIn("Чьи ссылки", text)
        self.assertFalse(
            any(b.callback_data.startswith("pref:senders:") for row in keyboard.inline_keyboard for b in row)
        )
        message = incoming(chat_id=10, sender=10)
        query = SimpleNamespace(
            message=message, from_user=message.from_user, data="pref:senders:self", answer=AsyncMock()
        )
        with patch("aiogram.types.Message.edit_text", new=AsyncMock()) as edit:
            await change_preference(query, app)
        self.assertNotIn("senders", self.database.preferences("user", 10))
        self.assertIn("автоматически", query.answer.call_args.args[0])
        self.assertFalse(
            any(
                b.callback_data.startswith("pref:senders:")
                for row in edit.call_args.kwargs["reply_markup"].inline_keyboard
                for b in row
            )
        )

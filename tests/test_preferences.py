import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.errors import MediaError
from core.models import RenderOptions
from handlers.preferences import change_preference
from services.application import Application
from services.delivery import TelegramDelivery
from tests.support import DatabaseFixture
from tests.test_handlers import incoming


class PreferenceTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.bot = SimpleNamespace(
            id=123,
            send_message=AsyncMock(),
            send_video=AsyncMock(),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )
        self.app = Application(self.settings, self.database, self.bot, None)

    async def test_default_result_is_identical_in_private_group_and_both_business_directions(self):
        self.database.set_group(-123, True)
        self.database.save_connection("conn", 99, True, True)
        self.database.touch_business_chat("conn", 10, time.time())
        messages = [
            incoming(),
            incoming(chat_id=-123),
            incoming(business="conn"),
            incoming(sender=99, business="conn").model_copy(update={"message_id": 2}),
        ]
        results = []
        for message in messages:
            destination = await self.app.destination(message)
            self.app.submit(destination, message.text)
            job = self.database.claim(parallel=True)
            results.append((job.cache_key, job.mode, job.render))
            self.database.complete_job(job.id)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(results[0][1:], ("video", RenderOptions()))

    async def test_personal_settings_follow_owner_and_do_not_affect_another_owner_or_group(self):
        self.database.set_preference("user", 99, "photo_seconds", 5)
        self.database.set_preference("user", 99, "live_limit", 10)
        self.database.set_preference("user", 99, "mode", "photos")
        self.database.save_connection("conn", 99, True, True)
        destination = await self.app.destination(incoming(business="conn"))
        self.app.submit(destination, "https://youtu.be/abc")
        job = self.database.claim()
        self.assertEqual(job.render, RenderOptions(5, 10))
        self.assertEqual(job.mode, "album")
        self.assertEqual(self.database.preferences("user", 20)["photo_seconds"], 3)
        self.assertEqual(self.database.preferences("group", -123)["live_limit"], 0)
        self.database.set_preference("user", 99, "photo_seconds", 2)
        self.assertEqual(self.database.get_job(job.id)["cache_key"], job.cache_key)
        self.assertEqual(job.render.photo_seconds, 5)
        self.database.complete_job(job.id)
        self.database.request_media(job.id, "photos", 100)
        self.assertEqual(self.database.claim().render, RenderOptions(5, 10))

    async def test_business_sender_filter_keeps_incoming_reply_window(self):
        self.database.save_connection("conn", 99, True, True)
        self.database.set_preference("user", 99, "senders", "self")
        self.assertIsNone(await self.app.destination(incoming(business="conn")))
        self.assertTrue(self.database.business_can_send("conn", 10))
        self.assertIsNotNone(await self.app.destination(incoming(sender=99, business="conn")))
        self.database.set_preference("user", 99, "senders", "peer")
        self.assertIsNone(await self.app.destination(incoming(sender=99, business="conn")))
        self.assertIsNotNone(await self.app.destination(incoming(business="conn")))

    async def test_own_links_are_not_handled_by_both_connections(self):
        self.database.save_connection("recipient", 99, True, True)
        self.database.save_connection("sender", 20, True, True)
        self.database.set_preference("user", 20, "senders", "self")
        self.database.touch_business_chat("sender", 99, time.time())
        self.assertIsNone(await self.app.destination(incoming(sender=20, business="recipient")))
        self.assertIsNotNone(await self.app.destination(incoming(sender=20, chat_id=99, business="sender")))
        self.database.save_connection("sender", 20, False, True)
        self.assertIsNotNone(await self.app.destination(incoming(sender=20, business="recipient")))

    async def test_group_filter_is_checked_again_before_sending(self):
        self.database.set_group(-123, True)
        destination = await self.app.destination(incoming(chat_id=-123))
        self.app.submit(destination, "https://youtu.be/abc")
        job = self.database.claim()
        self.database.only_sender(-123, 99)
        with self.assertRaises(MediaError):
            await TelegramDelivery(self.bot, self.database, self.settings).deliver(job, self.bundle())
        self.bot.send_video.assert_not_awaited()
        self.assertIsNone(await self.app.destination(incoming(chat_id=-123)))
        self.database.allow_sender(-123, 20, True)
        self.assertIsNotNone(await self.app.destination(incoming(chat_id=-123)))
        self.database.set_preference("group", -123, "senders", "admins")
        self.assertIsNone(await self.app.destination(incoming(chat_id=-123)))
        self.bot.get_chat_member.return_value.status = "administrator"
        self.assertIsNotNone(await self.app.destination(incoming(chat_id=-123)))

    async def test_group_preference_callback_rechecks_admin_and_is_scoped_to_this_group(self):
        message = incoming(chat_id=-123)
        query = SimpleNamespace(
            message=message, from_user=message.from_user, data="pref:photo_seconds:10", answer=AsyncMock()
        )
        await change_preference(query, self.app)
        self.assertEqual(self.database.preferences("group", -123)["photo_seconds"], 3)
        self.assertTrue(query.answer.call_args.kwargs["show_alert"])
        self.bot.get_chat_member.return_value.status = "administrator"
        from unittest.mock import patch

        with patch("aiogram.types.Message.edit_text", new=AsyncMock()):
            await change_preference(query, self.app)
        self.assertEqual(self.database.preferences("group", -123)["photo_seconds"], 10)
        self.assertEqual(self.database.preferences("group", -456)["photo_seconds"], 3)

    def test_default_cache_key_is_preserved_and_custom_render_is_separate(self):
        self.assertEqual(RenderOptions().cache_key("base"), "base")
        self.assertNotEqual(RenderOptions(5).cache_key("base"), RenderOptions(3, 5).cache_key("base"))

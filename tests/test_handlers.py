import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import BusinessBotRights, BusinessConnection, Chat, Message, User

from handlers.callbacks import media_action
from handlers.links import handle_links
from services.application import Application
from tests.support import DatabaseFixture


def incoming(text="https://youtu.be/abc", *, chat_id=10, sender=20, business=None, bot_sender=False):
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="supergroup" if chat_id < 0 else "private"),
        from_user=User(id=sender, is_bot=bot_sender, first_name="User"),
        text=text,
        business_connection_id=business,
    )


class HandlerTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.bot = SimpleNamespace(id=123, send_message=AsyncMock(), get_business_connection=AsyncMock())
        self.app = Application(self.settings, self.database, self.bot, None)

    async def test_private_message_queues_real_download(self):
        await handle_links(incoming(), self.app)
        self.assertEqual(self.database.claim().url, "https://www.youtube.com/watch?v=abc")
        self.bot.send_message.assert_awaited_once()

    async def test_plain_text_is_silent(self):
        await handle_links(incoming("Привет"), self.app)
        self.assertIsNone(self.database.claim())
        self.bot.send_message.assert_not_awaited()

    async def test_group_disabled_by_default(self):
        await handle_links(incoming(chat_id=-123), self.app)
        self.assertIsNone(self.database.claim())

    async def test_enabled_group_is_quiet_and_queues(self):
        self.database.set_group(-123, True)
        await handle_links(incoming(chat_id=-123), self.app)
        self.assertIsNotNone(self.database.claim())
        self.bot.send_message.assert_not_awaited()

    async def test_duplicate_update_is_idempotent(self):
        await handle_links(incoming(), self.app)
        await handle_links(incoming(), self.app)
        first = self.database.claim()
        self.database.status(first.id, "done")
        self.assertIsNone(self.database.claim())

    async def test_multiple_urls_preserve_order(self):
        await handle_links(incoming("https://youtu.be/abc https://vk.com/video-1_2"), self.app)
        first = self.database.claim()
        self.assertIn("youtube", first.url)
        self.database.status(first.id, "done")
        self.assertIn("vk.com", self.database.claim().url)

    async def test_business_owner_and_bot_messages_do_not_loop(self):
        self.database.save_connection("conn", 99, True, True)
        self.assertIsNone(await self.app.destination(incoming(sender=99, business="conn")))
        self.assertIsNone(await self.app.destination(incoming(sender=123, business="conn", bot_sender=True)))

    async def test_business_incoming_can_queue(self):
        self.database.save_connection("conn", 99, True, True)
        await handle_links(incoming(business="conn"), self.app)
        job = self.database.claim()
        self.assertEqual(job.destination.business_id, "conn")
        self.assertTrue(self.database.business_can_send("conn", 10))
        self.bot.send_message.assert_not_awaited()

    async def test_revoked_business_rights_are_enforced(self):
        self.database.save_connection("conn", 99, True, False)
        self.assertIsNone(await self.app.destination(incoming(business="conn")))

    async def test_unknown_business_connection_is_resolved(self):
        connection = BusinessConnection(
            id="conn",
            user=User(id=99, is_bot=False, first_name="Owner"),
            user_chat_id=99,
            date=datetime.now(UTC),
            is_enabled=True,
            rights=BusinessBotRights(can_reply=True),
        )
        self.bot.get_business_connection.return_value = connection
        self.assertIsNotNone(await self.app.destination(incoming(business="conn")))
        self.bot.get_business_connection.assert_awaited_once_with("conn")

    async def test_other_user_cannot_use_media_button(self):
        job = self.job()
        query = SimpleNamespace(
            data=f"m:{job.id}:audio",
            from_user=User(id=999, is_bot=False, first_name="Other"),
            message=incoming(),
            answer=AsyncMock(),
            id="callback",
        )
        await media_action(query, self.app)
        self.assertTrue(query.answer.call_args.kwargs.get("show_alert"))

    async def test_valid_button_enqueues_action(self):
        job = self.job()
        query = SimpleNamespace(
            data=f"m:{job.id}:audio",
            from_user=User(id=20, is_bot=False, first_name="Owner"),
            message=incoming(),
            answer=AsyncMock(),
            id="callback",
        )
        await media_action(query, self.app)
        self.database.status(job.id, "done")
        self.assertEqual(self.database.claim().mode, "audio")

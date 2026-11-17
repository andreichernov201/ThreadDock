import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import BusinessBotRights, BusinessConnection, Chat, Message, MessageEntity, User, Video

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
        self.bot.send_message.assert_not_awaited()

    async def test_unsupported_services_are_silent_in_private_chat(self):
        for value in ("https://example.org/post/123", "https://www.twitch.tv/videos/123"):
            with self.subTest(value=value):
                await handle_links(incoming(value), self.app)
                self.assertIsNone(self.database.claim())
                self.bot.send_message.assert_not_awaited()

    async def test_unsupported_link_does_not_block_supported_link(self):
        await handle_links(incoming("https://example.org/post/1 https://youtu.be/abc"), self.app)
        self.assertIsNotNone(self.database.claim())
        self.bot.send_message.assert_not_awaited()

    async def test_plain_text_is_silent(self):
        await handle_links(incoming("Привет"), self.app)
        self.assertIsNone(self.database.claim())
        self.bot.send_message.assert_not_awaited()

    async def test_video_with_hidden_source_link_does_not_download_again(self):
        message = incoming().model_copy(
            update={
                "text": None,
                "caption": "Already downloaded video",
                "video": Video(file_id="video", file_unique_id="unique", width=320, height=240, duration=1),
                "caption_entities": [MessageEntity(type="text_link", offset=0, length=7, url="https://youtu.be/abc")],
            }
        )
        await handle_links(message, self.app)
        self.assertIsNone(self.database.claim())
        self.bot.send_message.assert_not_awaited()

    async def test_ready_attachments_ignore_links_in_captions(self):
        for field in (
            "photo",
            "video",
            "animation",
            "document",
            "audio",
            "voice",
            "video_note",
            "sticker",
            "paid_media",
        ):
            with self.subTest(field=field):
                await handle_links(incoming().model_copy(update={field: True}), self.app)
                self.assertIsNone(self.database.claim())

    async def test_business_media_opens_reply_window_without_download(self):
        self.database.save_connection("conn", 99, True, True)
        await handle_links(incoming(business="conn").model_copy(update={"video": True}), self.app)
        self.assertTrue(self.database.business_can_send("conn", 10))
        self.assertIsNone(self.database.claim())
        await handle_links(incoming(sender=99, business="conn"), self.app)
        self.assertIsNotNone(self.database.claim())

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

    async def test_business_owner_links_are_accepted_inside_reply_window(self):
        self.database.save_connection("conn", 99, True, True)
        self.database.touch_business_chat("conn", 10, datetime.now(UTC).timestamp())
        await handle_links(incoming(sender=99, business="conn"), self.app)
        self.assertEqual(self.database.claim().destination.requester_id, 99)
        self.assertIsNone(await self.app.destination(incoming(sender=123, business="conn", bot_sender=True)))
        automated = incoming(sender=99, business="conn").model_copy(
            update={"sender_business_bot": User(id=123, is_bot=True, first_name="Bot")}
        )
        self.assertIsNone(await self.app.destination(automated))

    async def test_owner_outgoing_does_not_create_a_reply_window(self):
        self.database.save_connection("conn", 99, True, True)
        self.assertIsNone(await self.app.destination(incoming(sender=99, business="conn")))
        self.assertFalse(self.database.business_can_send("conn", 10))

    async def test_plain_incoming_message_opens_window_for_owner_link(self):
        self.database.save_connection("conn", 99, True, True)
        await handle_links(incoming(text="hello", sender=20, business="conn"), self.app)
        await handle_links(incoming(sender=99, business="conn"), self.app)
        self.assertIsNotNone(self.database.claim())

    async def test_owner_link_does_not_renew_incoming_window(self):
        self.database.save_connection("conn", 99, True, True)
        self.assertIsNone(await self.app.destination(incoming(sender=99, business="conn")))
        self.assertFalse(self.database.business_can_send("conn", 10))

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
        self.database.complete_job(job.id)
        query = SimpleNamespace(
            data=f"m:{job.id}:audio",
            from_user=User(id=20, is_bot=False, first_name="Owner"),
            message=incoming(),
            answer=AsyncMock(),
            id="callback",
        )
        await media_action(query, self.app)
        self.assertEqual(self.database.claim().mode, "audio")

    async def test_ten_button_clicks_only_enqueue_one_action(self):
        job = self.job()
        self.database.complete_job(job.id)
        for index in range(10):
            await media_action(
                SimpleNamespace(
                    data=f"m:{job.id}:audio",
                    from_user=incoming().from_user,
                    message=incoming(),
                    answer=AsyncMock(),
                    id=f"click-{index}",
                ),
                self.app,
            )
        action = self.database.claim()
        self.assertEqual(action.mode, "audio")
        self.database.complete_job(action.id)
        self.assertIsNone(self.database.claim())

    async def test_retry_clicks_reuse_failure_once(self):
        job = self.job(mode="photos")
        self.database.delivered_piece(job.id, 0, [123])
        self.database.status(job.id, "failed", "source_temporary")
        self.database.offer_retry(job.id)
        for index in range(10):
            await media_action(
                SimpleNamespace(
                    data=f"r:{job.id}:{job.attempts}",
                    from_user=incoming().from_user,
                    message=incoming(),
                    answer=AsyncMock(),
                    id=f"click-{index}",
                ),
                self.app,
            )
        retry = self.database.claim()
        self.assertEqual(retry.id, job.id)
        self.assertEqual(retry.mode, "photos")
        self.assertEqual(retry.progress, 1)
        self.database.complete_job(retry.id)
        self.assertIsNone(self.database.claim())

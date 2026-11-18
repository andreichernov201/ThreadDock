import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage, SendVideo

from core.errors import MediaError, MissingAsset
from core.models import Asset
from services.delivery import TelegramDelivery
from tests.support import DatabaseFixture


def sent(identifier=1, kind="video"):
    return SimpleNamespace(message_id=identifier, **{kind: SimpleNamespace(file_id=f"remote-{identifier}")})


class DeliveryTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.bot = SimpleNamespace(
            id=123,
            send_video=AsyncMock(return_value=sent()),
            send_media_group=AsyncMock(),
            send_document=AsyncMock(return_value=sent(kind="document")),
            send_audio=AsyncMock(return_value=sent(kind="audio")),
            send_message=AsyncMock(),
        )
        self.delivery = TelegramDelivery(self.bot, self.database, self.settings)

    async def test_upload_stores_file_id_and_checkpoint(self):
        job, bundle = self.job(), self.bundle()
        await self.delivery.deliver(job, bundle)
        self.assertEqual(self.database.file_id(123, job.cache_key, "video.mp4", "video"), "remote-1")
        self.assertEqual(self.database.get_job(job.id)["progress"], 1)

    async def test_cached_video_does_not_require_local_file(self):
        job, bundle = self.job(), self.bundle()
        (bundle.directory / "video.mp4").unlink()
        self.database.save_file_id(123, job.cache_key, "video.mp4", "video", "cached")
        await self.delivery.deliver(job, bundle)
        self.assertEqual(self.bot.send_video.call_args.kwargs["video"], "cached")

    async def test_stale_file_id_retries_from_disk(self):
        job, bundle = self.job(), self.bundle()
        self.database.save_file_id(123, job.cache_key, "video.mp4", "video", "expired")
        self.bot.send_video.side_effect = [
            TelegramBadRequest(method=SendVideo(chat_id=10, video="expired"), message="wrong file identifier"),
            sent(),
        ]
        await self.delivery.deliver(job, bundle)
        self.assertEqual(self.bot.send_video.await_count, 2)

    async def test_missing_asset_is_detected_before_sending(self):
        job, bundle = self.job(), self.bundle()
        (bundle.directory / "video.mp4").unlink()
        with self.assertRaises(MissingAsset):
            await self.delivery.deliver(job, bundle)
        self.bot.send_video.assert_not_awaited()
        self.assertEqual(self.database.get_job(job.id)["status"], "running")

    async def test_photos_are_batched_in_order_and_resumable(self):
        job, bundle = self.job(mode="photos"), self.bundle(photos=11)
        self.bot.send_media_group.return_value = [sent(index + 1, "document") for index in range(10)]
        await self.delivery.deliver(job, bundle)
        self.assertEqual(len(self.bot.send_media_group.call_args.kwargs["media"]), 10)
        self.bot.send_document.assert_awaited_once()
        self.assertEqual(self.database.get_job(job.id)["progress"], 2)

    async def test_disabled_group_prevents_delivery(self):
        job = self.job(chat_id=-123)
        with self.assertRaises(MediaError):
            await self.delivery.deliver(job, self.bundle())
        self.bot.send_video.assert_not_awaited()

    async def test_expired_album_never_starts_the_next_piece(self):
        job, bundle = self.job(mode="photos"), self.bundle(photos=11)

        async def accepted(**kwargs):
            self.database.connection.execute("UPDATE jobs SET active_elapsed=61 WHERE id=?", (job.id,))
            self.database.connection.commit()
            return [sent(index + 1, "document") for index in range(10)]

        self.bot.send_media_group.side_effect = accepted
        with self.assertRaises(MediaError) as caught:
            await self.delivery.deliver(job, bundle)
        self.assertEqual(caught.exception.code, "request_expired")
        self.assertEqual(self.database.get_job(job.id)["progress"], 1)
        self.bot.send_document.assert_not_awaited()

    async def test_business_route_carries_connection_and_reply(self):
        self.database.save_connection("conn", 99, True, True)

        self.database.touch_business_chat("conn", 10, time.time())
        job = self.job(business_id="conn")
        await self.delivery.deliver(job, self.bundle())
        options = self.bot.send_video.call_args.kwargs
        self.assertEqual(options["business_connection_id"], "conn")
        self.assertEqual(options["reply_parameters"].message_id, 1)

    async def test_unavailable_audio_has_clear_error(self):
        with self.assertRaisesRegex(MediaError, "нет"):
            await self.delivery.deliver(self.job(mode="audio"), self.bundle())

    async def test_album_controls_are_a_separate_reply_to_the_album(self):
        self.database.set_group(-123, True)
        job = self.job(chat_id=-123, mode="photos")
        job = replace(job, destination=replace(job.destination, thread_id=7))
        bundle = replace(self.bundle(photos=2), audio=Asset("audio.m4a", "audio", 4))
        self.bot.send_media_group.return_value = [sent(11, "document"), sent(12, "document")]
        await self.delivery.deliver(job, bundle)
        options = self.bot.send_message.call_args.kwargs
        self.assertEqual(options["chat_id"], -123)
        self.assertEqual(options["message_thread_id"], 7)
        self.assertEqual(options["reply_parameters"].message_id, 12)
        self.assertEqual(options["reply_markup"].inline_keyboard[0][0].callback_data, f"m:{job.id}:audio")
        self.assertEqual(self.database.get_job(job.id)["progress"], 1)

    async def test_album_controls_keep_business_connection(self):
        self.database.save_connection("conn", 99, True, True)
        self.database.touch_business_chat("conn", 10, time.time())
        job = self.job(business_id="conn", mode="photos")
        bundle = replace(self.bundle(photos=2), audio=Asset("audio.m4a", "audio", 4))
        self.bot.send_media_group.return_value = [sent(11, "document"), sent(12, "document")]
        await self.delivery.deliver(job, bundle)
        self.assertEqual(self.bot.send_message.call_args.kwargs["business_connection_id"], "conn")

    async def test_failed_control_message_never_replays_accepted_album(self):
        job = self.job(mode="photos")
        bundle = replace(self.bundle(photos=2), audio=Asset("audio.m4a", "audio", 4))
        self.bot.send_media_group.return_value = [sent(11, "document"), sent(12, "document")]
        self.bot.send_message.side_effect = TelegramNetworkError(
            method=SendMessage(chat_id=10, text="controls"), message="lost response"
        )
        await self.delivery.deliver(job, bundle)
        self.database.recover()
        await self.delivery.deliver(self.database.claim(), bundle)
        self.bot.send_media_group.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()

    async def test_album_without_extra_assets_has_no_control_message(self):
        job = self.job(mode="photos")
        self.bot.send_media_group.return_value = [sent(11, "document"), sent(12, "document")]
        await self.delivery.deliver(job, self.bundle(photos=2))
        self.bot.send_message.assert_not_awaited()

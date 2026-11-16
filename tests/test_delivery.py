import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendVideo

from core.errors import MediaError, MissingAsset
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

    async def test_business_route_carries_connection_and_reply(self):
        self.database.save_connection("conn", 99, True, True)
        import time

        self.database.touch_business_chat("conn", 10, time.time())
        job = self.job(business_id="conn")
        await self.delivery.deliver(job, self.bundle())
        options = self.bot.send_video.call_args.kwargs
        self.assertEqual(options["business_connection_id"], "conn")
        self.assertEqual(options["reply_parameters"].message_id, 1)

    async def test_unavailable_audio_has_clear_error(self):
        with self.assertRaisesRegex(MediaError, "нет"):
            await self.delivery.deliver(self.job(mode="audio"), self.bundle())

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import User

from core.errors import QueueFull
from handlers.callbacks import media_action
from services.application import Application
from tests.support import DatabaseFixture
from tests.test_handlers import incoming


class MediaActionTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.bot = SimpleNamespace(id=123, send_message=AsyncMock())
        self.app = Application(self.settings, self.database, self.bot, None)

    def query(self, job, mode="video", identifier="tap"):
        return SimpleNamespace(
            data=f"m:{job.id}:{mode}",
            from_user=User(id=20, is_bot=False, first_name="User"),
            message=incoming(),
            answer=AsyncMock(),
            id=identifier,
        )

    async def test_ten_retry_taps_produce_one_delivery_job(self):
        job = self.job()
        self.database.status(job.id, "failed", "source_temporary")
        self.database.offer_retry(job.id)
        for number in range(10):
            await media_action(self.query(job, identifier=f"tap-{number}"), self.app)
        queued = self.database.claim()
        self.assertEqual(queued.id, job.id)
        self.database.status(queued.id, "done")
        self.assertIsNone(self.database.claim())
        self.assertEqual(self.database.connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    async def test_completed_retry_button_cannot_resend_video(self):
        job = self.job()
        self.database.status(job.id, "done")
        query = self.query(job)
        await media_action(query, self.app)
        self.assertIsNone(self.database.claim())
        self.assertIn("отправлен", query.answer.call_args.args[0])

    async def test_alternative_format_is_deduplicated_after_success(self):
        job = self.job()
        self.database.status(job.id, "done")
        for number in range(10):
            await media_action(self.query(job, "audio", str(number)), self.app)
        audio = self.database.claim()
        self.assertEqual(audio.mode, "audio")
        self.database.status(audio.id, "done")
        await media_action(self.query(job, "audio", "later"), self.app)
        self.assertIsNone(self.database.claim())

    def test_retry_keeps_accepted_album_checkpoint(self):
        job = self.job(mode="photos")
        self.database.delivered_piece(job.id, 0, [11, 12])
        self.database.status(job.id, "uncertain")
        self.assertEqual(self.database.request_media(job.id, "photos", 100), "accepted")
        self.assertEqual(self.database.claim().progress, 1)

    def test_full_queue_does_not_reset_failed_job(self):
        job = self.job()
        self.database.status(job.id, "failed")
        self.job(chat_id=11)
        with self.assertRaises(QueueFull):
            self.database.request_media(job.id, "video", 1)
        self.assertEqual(self.database.get_job(job.id)["status"], "failed")

    def test_recovery_removes_legacy_callback_storm(self):
        job = self.job()
        self.database.status(job.id, "failed")
        for number in range(10):
            self.database.connection.execute(
                """INSERT INTO jobs(request_key,url,cache_key,chat_id,message_id,requester_id,mode,created_at,updated_at)
                SELECT ?,url,cache_key,chat_id,message_id,requester_id,mode,created_at,updated_at FROM jobs WHERE id=?""",
                (f"callback:old-{number}", job.id),
            )
        self.database.connection.commit()
        self.database.recover()
        first = self.database.claim()
        self.assertIsNotNone(first)
        self.database.status(first.id, "done")
        self.assertIsNone(self.database.claim())
        self.assertEqual(
            self.database.connection.execute("SELECT count(*) FROM jobs WHERE status='cancelled'").fetchone()[0],
            9,
        )

    def test_owner_outgoing_does_not_refresh_expired_window(self):
        self.database.save_connection("conn", 99, True, True)
        self.database.touch_business_chat("conn", 10, time.time() - 86401)
        self.assertFalse(self.app.delivery.permitted(self.job(business_id="conn").destination))

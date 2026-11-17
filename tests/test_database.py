import time
import unittest

from core.errors import QueueFull
from core.models import Destination, MediaBundle
from tests.support import DatabaseFixture


class DatabaseTests(DatabaseFixture, unittest.TestCase):
    def test_pending_same_video_in_one_chat_is_deduplicated(self):
        first = self.job()
        self.assertEqual(
            self.database.enqueue([("different-message", first.url, first.cache_key)], first.destination, "video", 100),
            0,
        )

    def test_success_cancels_legacy_duplicates_but_preserves_other_chats_and_modes(self):
        first = self.job()
        # Older releases admitted one job per click, with independent request keys.
        self.database.connection.execute(
            """INSERT INTO jobs(request_key,url,cache_key,chat_id,message_id,requester_id,mode,created_at,updated_at)
            SELECT 'legacy-duplicate',url,cache_key,chat_id,message_id,requester_id,mode,created_at,updated_at FROM jobs WHERE id=?""",
            (first.id,),
        )
        self.database.connection.commit()
        self.database.enqueue([("other-chat", first.url, first.cache_key)], Destination(11, 1, 20), "video", 100)
        self.database.enqueue([("audio", first.url, first.cache_key)], first.destination, "audio", 100)
        self.database.complete_job(first.id)
        duplicate = self.database.connection.execute(
            "SELECT * FROM jobs WHERE request_key='legacy-duplicate'"
        ).fetchone()
        self.assertEqual(duplicate["status"], "cancelled")
        other, audio = self.database.claim(), self.database.claim()
        self.assertEqual(other.destination.chat_id, 11)
        self.assertEqual(audio.mode, "audio")

    def test_retry_button_does_not_resurrect_completed_or_permanent_failure(self):
        job = self.job()
        self.database.status(job.id, "failed", "unavailable")
        self.assertFalse(self.database.retry_job(job.id, job.attempts, 100))
        self.database.offer_retry(job.id)
        self.assertTrue(self.database.retry_job(job.id, job.attempts, 100))
        self.assertFalse(self.database.retry_job(job.id, job.attempts, 100))
        retry = self.database.claim()
        self.database.status(retry.id, "failed", "source_temporary")
        self.database.offer_retry(retry.id)
        self.assertFalse(self.database.retry_job(job.id, job.attempts, 100))

    def test_restart_discards_backlog_delivered_before_restart(self):
        job = self.job()
        self.database.connection.execute(
            """INSERT INTO jobs(request_key,url,cache_key,chat_id,message_id,requester_id,mode,created_at,updated_at)
            SELECT 'old-queue',url,cache_key,chat_id,message_id,requester_id,mode,created_at,updated_at FROM jobs WHERE id=?""",
            (job.id,),
        )
        self.database.connection.commit()
        self.database.status(job.id, "done")
        self.database.recover()
        self.assertIsNone(self.database.claim())

    def test_message_deduplication(self):
        destination = Destination(1, 2, 3)
        requests = [("one", "https://youtu.be/abc", "key")]
        self.assertEqual(self.database.enqueue(requests, destination, "video", 10), 1)
        self.assertEqual(self.database.enqueue(requests, destination, "video", 10), 0)

    def test_queue_capacity_is_atomic(self):
        with self.assertRaises(QueueFull):
            self.database.enqueue([("a", "a", "a"), ("b", "b", "b")], Destination(1, 2, 3), "video", 1)
        self.assertIsNone(self.database.claim())

    def test_chat_order_and_parallel_chats(self):
        self.database.enqueue([("a", "a", "a"), ("b", "b", "b")], Destination(1, 2, 3), "video", 10)
        self.database.enqueue([("c", "c", "c")], Destination(2, 2, 3), "video", 10)
        first, parallel = self.database.claim(), self.database.claim()
        self.assertEqual(first.url, "a")
        self.assertEqual(parallel.url, "c")
        self.assertIsNone(self.database.claim())
        self.database.status(first.id, "done")
        self.assertEqual(self.database.claim().url, "b")

    def test_retry_does_not_let_next_url_overtake(self):
        self.database.enqueue([("a", "a", "a"), ("b", "b", "b")], Destination(1, 2, 3), "video", 10)
        job = self.database.claim()
        self.database.status(job.id, "queued", delay=10)
        self.assertIsNone(self.database.claim())

    def test_recovery_distinguishes_download_from_uncertain_send(self):
        first = self.job(chat_id=1)
        second = self.job(chat_id=2)
        self.database.status(second.id, "sending")
        self.assertEqual(self.database.recover(), 1)
        self.assertEqual(self.database.get_job(first.id)["status"], "queued")
        self.assertEqual(self.database.get_job(second.id)["status"], "uncertain")

    def test_delivery_checkpoint_is_durable(self):
        job = self.job()
        self.database.delivered_piece(job.id, 0, [11, 12])
        self.assertEqual(self.database.get_job(job.id)["progress"], 1)

    def test_cache_round_trip_and_expiration(self):
        bundle = self.bundle()
        self.database.put_cache("key", bundle, 60)
        self.assertEqual(self.database.get_cache("key"), bundle)
        self.database.connection.execute("UPDATE cache SET expires_at=0")
        self.database.connection.commit()
        self.assertIsNone(self.database.get_cache("key"))

    def test_file_id_is_scoped_to_bot_and_media_kind(self):
        self.database.save_file_id(1, "key", "video.mp4", "video", "remote")
        self.assertEqual(self.database.file_id(1, "key", "video.mp4", "video"), "remote")
        self.assertIsNone(self.database.file_id(2, "key", "video.mp4", "video"))
        self.assertIsNone(self.database.file_id(1, "key", "video.mp4", "document"))

    def test_group_settings_override_environment_defaults(self):
        self.assertTrue(self.database.group_enabled(-1, frozenset({-1})))
        self.database.set_group(-1, False)
        self.assertFalse(self.database.group_enabled(-1, frozenset({-1})))

    def test_business_rights_and_24_hour_window(self):
        self.database.save_connection("business", 10, True, True)
        self.database.touch_business_chat("business", 20, time.time())
        self.assertTrue(self.database.business_can_send("business", 20))
        self.database.save_connection("business", 10, True, False)
        self.assertFalse(self.database.business_can_send("business", 20))
        self.database.save_connection("old", 10, True, True)
        self.database.touch_business_chat("old", 20, time.time() - 86401)
        self.assertFalse(self.database.business_can_send("old", 20))

    def test_bot_binding_rejects_shared_directory(self):
        self.database.bind_bot(1)
        self.database.bind_bot(1)
        with self.assertRaises(RuntimeError):
            self.database.bind_bot(2)

    def test_manifest_cannot_escape_data_directory(self):
        with self.assertRaises(ValueError):
            MediaBundle.deserialize('{"directory":"../outside"}', self.root)

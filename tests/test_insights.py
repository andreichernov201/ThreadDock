import time
import unittest

from archive.analytics import statistics_for
from tests.test_archive import ArchiveFixture


class InsightTests(ArchiveFixture, unittest.TestCase):
    def test_all_aggregates_exclude_foreign_owner_and_chat(self):
        now = time.time()
        self.ingest(owner=10, body="our text", media=[self.photo()], archived=True)
        foreign = self.ingest(owner=20, body="FOREIGNSECRET", media=[self.photo()], archived=True)
        data = statistics_for(self.repo, 10, start=now - 1000, end=now + 1)
        self.assertEqual(data["totals"]["messages"], 1)
        self.assertEqual(data["insights"]["content"]["characters"], len("our text"))
        self.assertEqual(data["insights"]["media_types"][0]["files"], 1)
        self.assertEqual(sum(x["messages"] for x in data["insights"]["hourly"]), 1)
        self.assertEqual(data["insights"]["event_counts"], [{"kind": "deleted", "count": 1}])
        chat = self.repo.message(20, foreign)["chat_id"]
        with self.assertRaises(LookupError):
            statistics_for(self.repo, 10, chat_id=chat)

    def test_empty_period_has_no_made_up_response_time(self):
        data = statistics_for(self.repo, 10)
        self.assertEqual(data["insights"]["top_chats"], [])
        self.assertIsNone(data["insights"]["responses"]["incoming"]["median_seconds"])

    def test_event_period_is_distinct_from_send_period(self):
        now = time.time()
        self.ingest(at=now - 86400 * 40, archived=True)
        data = statistics_for(self.repo, 10, days=7)
        self.assertEqual(data["totals"]["messages"], 0)
        self.assertEqual(data["insights"]["event_counts"], [{"kind": "deleted", "count": 1}])

    def test_retention_keeps_period_counts_and_media_types_without_text(self):
        now = time.time()
        sent = now - 40 * 86400
        identifier = self.ingest(at=sent, body="старый текст", media=[self.photo()])
        self.ingest(
            at=sent, changed=sent + 1, body="редкий текст https://example.org", media=[self.photo()], archived=True
        )
        chat = self.repo.message(10, identifier)["chat_id"]
        self.ingest(owner=20, at=sent, body="other owner", media=[self.photo()])
        options = {"chat_id": chat, "start": sent - 60, "end": now + 60}
        before = statistics_for(self.repo, 10, **options)
        self.repo.update_owner(10, {"retention_days": 30})
        self.service.maintain()
        self.service.maintain()
        after = statistics_for(self.repo, 10, **options)
        for key in ("totals", "previous", "activity", "heatmap", "deletion_delays", "insights"):
            self.assertEqual(after[key], before[key], key)
        self.assertTrue(before["frequent_deleted_words"])
        self.assertEqual(after["frequent_deleted_words"], [])
        self.assertIsNone(self.repo.message(10, identifier))
        self.assertEqual(self.repo.events(10)["items"], [])
        self.assertEqual(self.repo.gallery(10)["items"], [])
        self.assertEqual(self.repo.search(10, "редкий")["items"], [])
        self.assertEqual(statistics_for(self.repo, 20, days=50)["totals"]["messages"], 1)

    def test_optout_and_full_owner_deletion_remove_retained_statistics(self):
        self.ingest(owner=10, media=[self.photo()], archived=True)
        self.ingest(owner=20, archived=True)
        self.repo.clear_history(10)
        self.repo.clear_history(20)
        self.assertEqual(statistics_for(self.repo, 10)["totals"]["messages"], 1)
        self.repo.purge_owner(20)
        self.assertEqual(statistics_for(self.repo, 20)["totals"]["messages"], 0)
        self.repo.set_author_optout(30, True)
        self.assertEqual(statistics_for(self.repo, 10)["totals"]["messages"], 0)
        self.assertEqual(statistics_for(self.repo, 10)["insights"]["media_types"], [])

    def test_statistics_reset_counts_only_new_messages_even_after_clearing_history(self):
        now = time.time()
        self.ingest(telegram_id=1, at=now - 100, media=[self.photo("gone")])
        self.repo.clear_history(10)
        saved = self.ingest(telegram_id=2, at=now - 80, media=[self.photo("saved")])
        self.repo.reset_statistics(10)
        self.ingest(telegram_id=3, at=now + 1, media=[self.photo("new")])
        window = {"start": now - 200, "end": now + 10}
        self.assertIsNotNone(self.repo.message(10, saved))
        self.assertEqual(statistics_for(self.repo, 10, **window)["totals"]["messages"], 1)
        self.repo.clear_history(10)
        result = statistics_for(self.repo, 10, **window)
        self.assertEqual(result["totals"]["messages"], 1)
        self.assertEqual(result["insights"]["media_types"][0]["files"], 1)

    def test_late_delete_updates_cleared_message_statistics_without_restoring_history(self):
        identifier = self.ingest(media=[self.photo()])
        self.repo.purge_message(10, identifier)
        self.assertEqual(self.repo.delete_messages(10, "connection-10", 30, [1], "Test chat"), 1)
        self.assertEqual(self.repo.delete_messages(10, "connection-10", 30, [1], "Test chat"), 0)
        self.assertIsNone(self.ingest(body="late reply copy", captured_from_reply=True))
        self.assertEqual(self.repo.events(10)["items"], [])
        self.assertEqual(statistics_for(self.repo, 10)["totals"]["messages"], 1)
        self.assertEqual(statistics_for(self.repo, 10)["totals"]["deleted"], 1)

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

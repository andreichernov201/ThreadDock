import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.errors import MediaError
from services.comments import TikTokComments, comment_caption, selected_comment
from services.links import normalize_url


class CommentTests(unittest.TestCase):
    def test_only_exact_comment_and_publication_are_used(self):
        data = {
            "status_code": 0,
            "comments": [
                {"cid": "99", "text": "wrong comment"},
                {"cid": "12", "aweme_id": "456", "text": "wrong video"},
                {"cid": "12", "aweme_id": "123", "text": " <b>Hello</b> 🔥\nreply "},
            ],
        }
        self.assertEqual(selected_comment(json.dumps(data).encode(), "12", "123"), "<b>Hello</b> 🔥\nreply")

    def test_missing_deleted_and_malformed_comments_have_no_caption(self):
        for data in [
            b"not json",
            b"null",
            b"[]",
            b'{"status_code":1}',
            b'{"status_code":0,"comments":[{"cid":"12","text":null}]}',
        ]:
            with self.subTest(data=data):
                self.assertEqual(selected_comment(data, "12", "123"), "")

    def test_caption_fits_telegram_with_emoji_and_does_not_split_surrogates(self):
        caption = comment_caption("🔥" * 1000)
        self.assertLessEqual(len(caption.encode("utf-16-le")), 2048)
        self.assertTrue(caption.endswith("…"))

    def test_request_identity_keeps_distinct_comments_separate(self):
        base = normalize_url("https://www.tiktok.com/@one/video/123")
        first = normalize_url(base.url + "?share_comment_id=12")
        second = normalize_url(base.url + "?comment_id=13")
        self.assertEqual(len({base.cache_key, first.cache_key, second.cache_key}), 3)
        self.assertEqual(first.without_comment.cache_key, base.cache_key)
        self.assertEqual(first.cache_key, normalize_url(base.url + "?comment_id=12").cache_key)
        self.assertEqual(normalize_url(base.url + "?comment_id=evil").comment_id, "")


class CommentHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_link_retains_selected_comment(self):
        target = "https://www.tiktok.com/@one/video/123?share_comment_id=12"
        http = SimpleNamespace(get_browser=AsyncMock(return_value=SimpleNamespace(url=target)))
        resolved = await TikTokComments(http).resolve(normalize_url("https://vt.tiktok.com/abc/"))
        self.assertEqual(resolved.comment_id, "12")
        self.assertEqual(resolved.without_comment.url, "https://www.tiktok.com/@one/video/123")

    async def test_unrelated_or_homepage_redirect_does_not_change_the_request(self):
        link = normalize_url("https://vt.tiktok.com/abc/")
        for target in ["https://www.youtube.com/watch?v=abc", "https://www.tiktok.com/", "https://evil.test/a"]:
            http = SimpleNamespace(get_browser=AsyncMock(return_value=SimpleNamespace(url=target)))
            self.assertEqual(await TikTokComments(http).resolve(link), link)

    async def test_first_party_lookup_and_optional_failure(self):
        link = normalize_url("https://www.tiktok.com/@one/video/123?share_comment_id=12")
        payload = json.dumps({"status_code": 0, "comments": [{"cid": "12", "aweme_id": "123", "text": "Hello"}]})
        http = SimpleNamespace(get_browser=AsyncMock(return_value=SimpleNamespace(body=payload.encode())))
        adapter = TikTokComments(http)
        self.assertEqual(await adapter.text(link), "Hello")
        url = http.get_browser.call_args.args[0]
        self.assertTrue(url.startswith("https://www.tiktok.com/api/comment/list/?"))
        self.assertIn("comment_id=12", url)
        http.get_browser.side_effect = TimeoutError
        self.assertEqual(await adapter.text(link), "")
        http.get_browser.side_effect = MediaError("Blocked", code="source_http")
        self.assertEqual(await adapter.text(link), "")

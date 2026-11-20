import importlib.util
import json
import unittest
from dataclasses import replace
from http.cookiejar import Cookie, MozillaCookieJar
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.errors import MediaError
from services.instagram import InstagramEmbed, InstagramGalleryError, parse_embed, video_post_id
from services.links import normalize_url
from tests.support import DatabaseFixture


def page(media):
    data = {"gql_data": {"shortcode_media": media}}
    return "<script>" + json.dumps({"contextJSON": json.dumps(data)}) + "</script>"


def video():
    return {
        "shortcode": "wanted",
        "is_video": True,
        "video_url": "https://cdninstagram.com/video.mp4",
        "display_url": "https://cdninstagram.com/cover.jpg",
    }


class EmbedParserTests(unittest.TestCase):
    def test_video_is_not_replaced_with_its_cover(self):
        entries = parse_embed(page(video()), "wanted", 100)
        self.assertEqual(entries[0][0], "https://cdninstagram.com/video.mp4")
        self.assertEqual(entries[0][1]["type"], "video")
        incomplete = video()
        del incomplete["video_url"]
        with self.assertRaises(MediaError):
            parse_embed(page(incomplete), "wanted", 100)

    def test_wrong_post_and_empty_embed_are_rejected(self):
        for html in (page(video()), '<script>{"contextJSON": "null"}</script>', "<html>No post</html>"):
            with self.subTest(html=html), self.assertRaises(MediaError):
                parse_embed(html, "another", 100)

    def test_mixed_carousel_preserves_every_member_and_order(self):
        still = {"is_video": False, "display_url": "https://cdninstagram.com/photo.jpg"}
        media = {"shortcode": "wanted", "edge_sidecar_to_children": {"edges": [{"node": still}, {"node": video()}]}}
        entries = parse_embed(page(media), "wanted", 100)
        self.assertEqual([meta["type"] for _, meta in entries], ["image", "video"])
        del media["edge_sidecar_to_children"]["edges"][1]["node"]["video_url"]
        with self.assertRaises(InstagramGalleryError):
            parse_embed(page(media), "wanted", 100)

    def test_limit_is_not_silent_truncation(self):
        media = {"shortcode": "wanted", "edge_sidecar_to_children": {"edges": [{"node": video()}, {"node": video()}]}}
        with self.assertRaises(MediaError) as caught:
            parse_embed(page(media), "wanted", 1)
        self.assertEqual(caught.exception.code, "photo_count")

    def test_unsafe_media_url_is_rejected(self):
        media = video()
        media["video_url"] = "http://127.0.0.1/private"
        with self.assertRaises(MediaError):
            parse_embed(page(media), "wanted", 100)

    def test_only_single_post_video_routes_are_eligible(self):
        for path in ("/reel/wanted/", "/reels/wanted/", "/tv/wanted/", "/author/reel/wanted/"):
            self.assertEqual(video_post_id("https://www.instagram.com" + path), "wanted")
        for path in ("/author/", "/stories/author/123/", "/reel/wanted/other", "/p/wanted/"):
            self.assertIsNone(video_post_id("https://www.instagram.com" + path))


@unittest.skipUnless(importlib.util.find_spec("curl_cffi"), "curl_cffi media extra is not installed")
class EmbedHttpTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def response(self, body=b"", status=200):
        async def chunks():
            yield body

        return SimpleNamespace(
            status_code=status, headers={"Location": "https://other.example/"}, aiter_content=chunks, aclose=AsyncMock()
        )

    def context(self, response):
        context = AsyncMock()
        session = SimpleNamespace(get=AsyncMock(return_value=response))
        context.__aenter__.return_value = session
        return context, session

    async def test_only_instagram_cookies_reach_fixed_origin_and_original_is_unchanged(self):
        path = self.root / "session.txt"
        jar = MozillaCookieJar(str(path))
        for domain, name in (
            (".instagram.com", "sessionid"),
            (".other.example", "unrelated"),
            (".evilinstagram.com", "wrong"),
        ):
            jar.set_cookie(
                Cookie(0, name, "fixture", None, False, domain, True, True, "/", True, True, None, True, None, None, {})
            )
        jar.save(ignore_discard=True)
        original = path.read_bytes()
        response = self.response(page(video()).encode())
        context, session = self.context(response)
        embed = InstagramEmbed(
            replace(self.settings, cookies_file=path), SimpleNamespace(proxy=SimpleNamespace(url="http://guard"))
        )
        with patch("curl_cffi.requests.AsyncSession", return_value=context) as factory:
            entries = await embed.entries(normalize_url("https://www.instagram.com/reel/wanted/"))
        self.assertEqual([cookie.name for cookie in factory.call_args.kwargs["cookies"]], ["sessionid"])
        self.assertEqual(factory.call_args.kwargs["proxy"], "http://guard")
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertEqual(session.get.call_args.args[0], "https://www.instagram.com/p/wanted/embed/captioned/")
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertEqual(len(entries), 1)
        response.aclose.assert_awaited_once()
        self.assertEqual(path.read_bytes(), original)

    async def test_redirects_and_oversized_responses_are_closed_without_following(self):
        for response in (self.response(status=302), self.response(b"x" * (4 * 1024**2 + 1))):
            context, session = self.context(response)
            embed = InstagramEmbed(self.settings, SimpleNamespace(proxy=SimpleNamespace(url="http://guard")))
            with patch("curl_cffi.requests.AsyncSession", return_value=context), self.assertRaises(MediaError):
                await embed.entries(normalize_url("https://www.instagram.com/reel/wanted/"))
            session.get.assert_awaited_once()
            response.aclose.assert_awaited_once()

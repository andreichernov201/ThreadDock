import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.errors import MediaError
from services.links import LinkError, normalize_url
from services.youtube_posts import YouTubePosts, original_image, parse_post

ID = "UgkxFixture_12345"
URL = "https://www.youtube.com/post/" + ID


def image(name):
    return {
        "backstageImageRenderer": {
            "image": {
                "thumbnails": [
                    {"url": f"https://yt3.ggpht.com/{name}=s288-c-fcrop64=1,0-rw", "width": 288, "height": 288},
                    {"url": f"https://yt3.ggpht.com/{name}=s1600-c-fcrop64=1,0-rw", "width": 1600, "height": 1600},
                ]
            }
        }
    }


def page(attachment, *, identifier=ID, others=()):
    post = {
        "backstagePostRenderer": {
            "postId": identifier,
            "backstageAttachment": attachment,
            "authorThumbnail": {"thumbnails": [{"url": "https://yt3.ggpht.com/avatar=s32"}]},
        }
    }
    return "<script>var ytInitialData = " + json.dumps({"contents": [post, *others]}) + ";</script>"


class YouTubePostParserTests(unittest.TestCase):
    def test_complete_carousel_has_full_images_in_original_order(self):
        html = page({"postMultiImageRenderer": {"images": [image(str(i)) for i in range(8)]}})
        entries = parse_post(html, ID, 100)
        self.assertEqual([url for url, _ in entries], [f"https://yt3.ggpht.com/{i}=s0-d" for i in range(8)])
        self.assertTrue(all(meta == {"type": "image", "extension": "jpg"} for _, meta in entries))

    def test_single_image_is_supported(self):
        entries = parse_post(page(image("original")), ID, 100)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0], "https://yt3.ggpht.com/original=s0-d")

    def test_other_posts_avatars_video_thumbnails_and_polls_are_not_used(self):
        other = {"backstagePostRenderer": {"postId": "another", "backstageAttachment": image("wrong")}}
        html = page({"postMultiImageRenderer": {"images": [image("right")]}}, others=[other])
        self.assertEqual(parse_post(html, ID, 100)[0][0], "https://yt3.ggpht.com/right=s0-d")
        for attachment in ({}, {"videoRenderer": {"thumbnail": image("video")}}, {"pollRenderer": image("poll")}):
            with self.subTest(attachment=attachment), self.assertRaises(MediaError) as caught:
                parse_post(page(attachment, others=[other]), ID, 100)
            self.assertEqual(caught.exception.code, "no_photos")

    def test_missing_requested_post_or_consent_page_is_unavailable(self):
        for html in (
            "<html>Before you continue</html>",
            "ytInitialData = null;",
            page(image("wrong"), identifier="another"),
        ):
            with self.subTest(html=html), self.assertRaises(MediaError) as caught:
                parse_post(html, ID, 100)
            self.assertEqual(caught.exception.code, "unavailable")

    def test_malformed_carousel_is_never_silently_truncated(self):
        for images in (
            [image("first"), {}],
            [image("first"), {"backstageImageRenderer": {"image": {"thumbnails": []}}}],
            [],
        ):
            with self.subTest(images=images), self.assertRaises(MediaError):
                parse_post(page({"postMultiImageRenderer": {"images": images}}), ID, 100)

    def test_maximum_photo_count_is_enforced(self):
        with self.assertRaises(MediaError) as caught:
            parse_post(page({"postMultiImageRenderer": {"images": [image("first"), image("second")]}}), ID, 1)
        self.assertEqual(caught.exception.code, "photo_count")

    def test_duplicate_images_remain_part_of_the_post(self):
        self.assertEqual(
            len(parse_post(page({"postMultiImageRenderer": {"images": [image("same"), image("same")]}}), ID, 100)), 2
        )

    def test_alternate_initial_data_assignment(self):
        html = page(image("original")).replace("var ytInitialData", 'window["ytInitialData"]')
        self.assertEqual(len(parse_post(html, ID, 100)), 1)

    def test_image_url_guards_and_original_rendition(self):
        self.assertEqual(
            original_image("//yt3.googleusercontent.com/img=w640-h640-c"), "https://yt3.googleusercontent.com/img=s0-d"
        )
        self.assertEqual(original_image("https://i.ytimg.com/img.jpg"), "https://i.ytimg.com/img.jpg")
        for url in (
            "https://127.0.0.1/a",
            "http://yt3.ggpht.com/a",
            "https://yt3.ggpht.com.evil.test/a",
            "https://yt3.ggpht.com:444/a",
        ):
            with self.subTest(url=url), self.assertRaises((MediaError, ValueError)):
                original_image(url)

    def test_post_identity_ignores_sharing_parameters_and_legacy_link_shape(self):
        variants = [
            URL,
            URL + "/?surface=shorts&data=tracking&si=tracking",
            "https://m.youtube.com/post/" + ID,
            "https://www.youtube.com/@creator/community?lb=" + ID,
            "https://www.youtube.com/channel/UCfixture/posts?lb=" + ID,
        ]
        links = [normalize_url(url) for url in variants]
        self.assertEqual({link.url for link in links}, {URL})
        self.assertEqual(len({link.cache_key for link in links}), 1)
        self.assertNotEqual(links[0].cache_key, normalize_url("https://youtube.com/watch?v=" + ID).cache_key)
        for url in ("https://youtube.com/post/short", URL + "/other"):
            with self.subTest(url=url), self.assertRaises(LinkError):
                normalize_url(url)


class YouTubePostHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_fixed_origin_request_and_bounded_optional_failure(self):
        http = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(body=page(image("first")).encode())))
        adapter = YouTubePosts(http, 100)
        entries = await adapter.entries(normalize_url(URL + "?data=tracking"))
        self.assertEqual(len(entries), 1)
        http.get.assert_awaited_once_with(URL + "?hl=en&ucbcb=1", 8 * 1024**2)
        http.get.side_effect = TimeoutError
        with self.assertRaises(MediaError) as caught:
            await adapter.entries(normalize_url(URL))
        self.assertTrue(caught.exception.retryable)

import unittest
from dataclasses import dataclass

from services.links import LinkError, extract_urls, normalize_url


@dataclass
class Entity:
    type: str
    offset: int
    length: int
    url: str | None = None


class ExtractUrlsTests(unittest.TestCase):
    def test_surrounding_text_order_and_duplicates(self):
        text = "Первое https://youtu.be/abc, второе https://vk.com/video-1_2! https://youtu.be/abc"
        self.assertEqual(extract_urls(text), ["https://youtu.be/abc", "https://vk.com/video-1_2"])

    def test_no_urls(self):
        self.assertEqual(extract_urls("Привет!"), [])

    def test_www_without_scheme(self):
        self.assertEqual(extract_urls("www.youtube.com/watch?v=abc"), ["www.youtube.com/watch?v=abc"])

    def test_balanced_parentheses(self):
        self.assertEqual(extract_urls("(https://vimeo.com/test_(one))."), ["https://vimeo.com/test_(one)"])

    def test_telegram_utf16_offsets_after_emoji(self):
        url = "https://youtu.be/abc"
        self.assertEqual(extract_urls("🔥 " + url, [Entity("url", 3, len(url))]), [url])

    def test_hidden_link_takes_precedence_over_visible_url(self):
        text = "https://x.com/user/status/1"
        target = "https://youtu.be/abc"
        self.assertEqual(extract_urls(text, [Entity("text_link", 0, len(text), target)]), [target])

    def test_hidden_and_plain_urls_keep_message_order(self):
        text = "ролик https://vk.com/video-1_2"
        self.assertEqual(
            extract_urls(text, [Entity("text_link", 0, 5, "https://youtu.be/abc")]),
            ["https://youtu.be/abc", "https://vk.com/video-1_2"],
        )

    def test_broken_entity_is_ignored(self):
        self.assertEqual(extract_urls("🔥", [Entity("url", 1, 1)]), [])


class NormalizeUrlTests(unittest.TestCase):
    def test_youtube_publication_identity_ignores_timestamps_and_url_shape(self):
        variants = [
            "https://youtu.be/abc?t=10",
            "https://www.youtube.com/watch?v=abc",
            "https://youtube.com/shorts/abc",
        ]
        self.assertEqual(len({normalize_url(url).cache_key for url in variants}), 1)

    def test_tiktok_video_and_photo_routes_share_post_identity(self):
        self.assertEqual(
            normalize_url("https://www.tiktok.com/@one/video/123").cache_key,
            normalize_url("https://www.tiktok.com/@two/photo/123?share=1").cache_key,
        )

    def test_youtube_short_url_and_tracking(self):
        link = normalize_url(" http://youtu.be/abc?si=tracking&t=10&utm_source=test#fragment ")
        self.assertEqual(link.url, "https://www.youtube.com/watch?v=abc&t=10")
        self.assertEqual(link.platform, "YouTube")

    def test_playlist_parameter_removed_from_single_video(self):
        self.assertEqual(
            normalize_url("https://m.youtube.com/watch?v=abc&list=123&index=2").url,
            "https://www.youtube.com/watch?v=abc",
        )

    def test_platforms(self):
        cases = {
            "https://vm.tiktok.com/example/": "TikTok",
            "https://www.instagram.com/reel/abc/": "Instagram",
            "https://youtube.com/shorts/abc": "YouTube",
            "https://vkvideo.ru/video-1_2": "VK",
            "https://twitter.com/user/status/1": "X",
            "vimeo.com/123": "Vimeo",
        }
        for url, platform in cases.items():
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url).platform, platform)

    def test_unsafe_and_unsupported_urls_are_rejected(self):
        cases = [
            "file:///etc/passwd",
            "ftp://youtube.com/video",
            "javascript:alert(1)",
            "https://127.0.0.1/video",
            "https://[::1]/video",
            "https://2130706433/video",
            "https://youtube.com.evil.example/video",
            "https://evil-youtube.com/video",
            "https://user:password@youtube.com/watch?v=1",
            "https://youtube.com:444/video",
            "https://youtube.com:0/video",
            "https://youtube.com:bad/video",
            "https://youtube.com\\@evil.example/video",
            "https://youtube.com./video",
            "https://youtube.com/%0d%0a",
            "https://youtube.com/\nwatch?v=1",
            "https://youtube.com/playlist?list=abc",
            "https://youtube.com",
            "",
        ]
        for url in cases:
            with self.subTest(url=url), self.assertRaises(LinkError):
                normalize_url(url)


if __name__ == "__main__":
    unittest.main()

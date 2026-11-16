import json
import unittest

from core.errors import MediaError
from services.tiktok import parse_photo_page


def page(item):
    state = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": item}}}}
    return '<script type="application/json" id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps(state) + "</script>"


class PhotoMetadataTests(unittest.TestCase):
    def test_order_music_and_title(self):
        post = parse_photo_page(
            page(
                {
                    "id": "123",
                    "desc": "Album",
                    "imagePost": {
                        "images": [
                            {"imageURL": {"urlList": ["https://cdn.example/first.jpg"]}},
                            {"imageURL": {"urlList": ["https://cdn.example/second.jpg"]}},
                        ]
                    },
                    "music": {"playUrl": "https://cdn.example/music.mp3"},
                }
            ),
            "123",
        )
        self.assertEqual(post.images[0], ("https://cdn.example/first.jpg",))
        self.assertEqual(post.images[1], ("https://cdn.example/second.jpg",))
        self.assertEqual(post.music, ("https://cdn.example/music.mp3",))
        self.assertEqual(post.title, "Album")

    def test_video_is_not_misclassified_as_photos(self):
        self.assertIsNone(parse_photo_page(page({"id": "123", "video": {"playAddr": "url"}}), "123"))

    def test_unrelated_post_is_ignored(self):
        self.assertIsNone(
            parse_photo_page(
                page(
                    {
                        "id": "999",
                        "imagePost": {"images": [{"imageURL": {"urlList": ["https://cdn.example/first.jpg"]}}]},
                    }
                ),
                "123",
            )
        )

    def test_no_music_is_supported(self):
        post = parse_photo_page(
            page(
                {"id": "123", "imagePost": {"images": [{"imageURL": {"urlList": ["https://cdn.example/first.jpg"]}}]}}
            ),
            "123",
        )
        self.assertEqual(post.music, ())

    def test_missing_image_url_does_not_silently_drop_a_slide(self):
        with self.assertRaises(MediaError):
            parse_photo_page(page({"id": "123", "imagePost": {"images": [{"imageURL": {}}]}}), "123")

    def test_invalid_json_is_handled(self):
        self.assertIsNone(parse_photo_page('<script id="SIGI_STATE">broken</script>'))

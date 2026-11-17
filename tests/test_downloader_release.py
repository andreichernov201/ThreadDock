import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import InputMediaPhoto, InputMediaVideo

from core.errors import MediaError
from core.models import Asset
from handlers.callbacks import media_action
from services.application import Application
from services.delivery import TelegramDelivery
from services.gallery import gallery_target, parse_gallery
from services.links import normalize_url
from services.media import MediaInfo
from services.pipeline import MediaPipeline
from services.tiktok import PhotoPost, TikTokPhotos, parse_photo_page, parse_video_page
from tests.support import DatabaseFixture
from tests.test_handlers import incoming


class PhotoFallbackTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.media = SimpleNamespace(
            probe=AsyncMock(return_value=MediaInfo(0, 100, 100, "mjpeg", "")),
            photo=AsyncMock(side_effect=lambda path, directory: Asset(path.name, "photo")),
            slideshow=AsyncMock(return_value=(Asset("video.mp4", "video"), True)),
            extract_audio=AsyncMock(return_value=Asset("audio.m4a", "audio", 20)),
        )
        self.photos = TikTokPhotos(self.settings, None, self.media)

        async def download(urls, target, limit):
            target.write_bytes(b"fixture")
            return "image/jpeg"

        self.photos._download_candidates = AsyncMock(side_effect=download)
        self.post = PhotoPost("Post", (("https://cdn.example/1",),), ())
        self.link = normalize_url("https://www.tiktok.com/@person/photo/123")

    async def test_no_music_returns_photo_without_rendering(self):
        bundle = await self.photos._render_post(self.link, self.root, self.post)
        self.assertIsNone(bundle.video)
        self.assertEqual(bundle.items("video")[0].kind, "photo")
        self.media.slideshow.assert_not_awaited()

    async def test_unavailable_music_does_not_discard_photos(self):
        async def download(urls, target, limit):
            if "music" in urls[0]:
                raise MediaError("denied", code="source_http")
            target.write_bytes(b"fixture")
            return "image/jpeg"

        self.photos._download_candidates.side_effect = download
        bundle = await self.photos._render_post(
            self.link, self.root, replace(self.post, music=("https://cdn.example/music",))
        )
        self.assertEqual(len(bundle.originals), 1)
        self.assertIsNone(bundle.audio)
        self.assertIsNone(bundle.video)

    async def test_rendering_failure_keeps_photos_and_audio(self):
        self.media.slideshow.side_effect = MediaError("encode", code="conversion_failed")
        bundle = await self.photos._render_post(
            self.link, self.root, replace(self.post, music=("https://cdn.example/music",))
        )
        self.assertIsNone(bundle.video)
        self.assertIsNotNone(bundle.audio)
        self.assertEqual(len(bundle.originals), 1)

    async def test_photo_story_is_never_rendered(self):
        bundle = await self.photos._render_post(
            self.link, self.root, replace(self.post, story=True, music=("https://cdn.example/music",))
        )
        self.assertIsNone(bundle.video)
        self.media.slideshow.assert_not_awaited()

    def test_motion_metadata_keeps_photo_and_video_urls_separate(self):
        data = {
            "id": "123",
            "imagePost": {
                "images": [
                    {
                        "imageURL": {"urlList": ["https://cdn.example/still"]},
                        "video": {"playAddr": {"urlList": ["https://cdn.example/motion"]}},
                    }
                ]
            },
        }
        post = parse_photo_page('<script id="SIGI_STATE">' + json.dumps(data) + "</script>", "123")
        self.assertEqual(post.images[0], ("https://cdn.example/still",))
        self.assertEqual(post.motion[0], ("https://cdn.example/motion",))


class GalleryMetadataTests(unittest.TestCase):
    def test_tiktok_page_fallback_selects_playback_not_dynamic_cover(self):
        post = {
            "id": "123",
            "video": {
                "dynamicCover": "https://cdn.example/cover.webp",
                "playAddr": "https://cdn.example/low.mp4",
                "bitrateInfo": [
                    {"Bitrate": 2000, "PlayAddr": {"UrlList": ["https://cdn.example/high.mp4"]}},
                    {"Bitrate": 1000, "PlayAddr": {"UrlList": ["https://cdn.example/mid.mp4"]}},
                ],
            },
        }
        page = '<script id="SIGI_STATE">' + json.dumps(post) + "</script>"
        _, urls = parse_video_page(page, "123")
        self.assertEqual(urls[0], "https://cdn.example/high.mp4")
        self.assertNotIn("https://cdn.example/cover.webp", urls)
        self.assertIsNone(parse_video_page(page, "999"))

    def test_story_target_filters_to_requested_id(self):
        url, identifier = gallery_target(normalize_url("https://www.tiktok.com/@user/story/123"))
        self.assertEqual(url, "https://www.tiktok.com/@user/stories")
        self.assertEqual(identifier, "123")
        payload = json.dumps(
            [[3, "https://cdn.example/1", {"id": "122"}], [3, "https://cdn.example/2", {"id": "123"}]]
        ).encode()
        self.assertEqual(parse_gallery(payload, 100, identifier), [("https://cdn.example/2", {"id": "123"})])

    def test_embedded_extractor_error_is_not_success(self):
        with self.assertRaises(MediaError):
            parse_gallery(b'[[-1,{"error":"NotFoundError","message":"unavailable"}]]', 100)

    def test_gallery_cannot_silently_truncate(self):
        payload = json.dumps([[3, "https://cdn.example/1", {}], [3, "https://cdn.example/2", {}]]).encode()
        with self.assertRaises(MediaError):
            parse_gallery(payload, 1)


class ReleaseDeliveryTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()

        def photo(identifier):
            return SimpleNamespace(message_id=identifier, photo=[SimpleNamespace(file_id=f"photo-{identifier}")])

        self.bot = SimpleNamespace(
            id=123,
            send_photo=AsyncMock(return_value=photo(11)),
            send_video=AsyncMock(
                return_value=SimpleNamespace(message_id=12, video=SimpleNamespace(file_id="video-12"))
            ),
            send_media_group=AsyncMock(
                return_value=[photo(11), SimpleNamespace(message_id=12, video=SimpleNamespace(file_id="video-12"))]
            ),
            edit_message_reply_markup=AsyncMock(),
        )
        self.delivery = TelegramDelivery(self.bot, self.database, self.settings)

    async def test_mixed_album_preserves_types_order_and_file_ids(self):
        job, bundle = self.job(), self.bundle(photos=1)
        still = replace(bundle.originals[0], kind="photo")
        bundle = replace(bundle, media=(still, bundle.video), audio=None)
        await self.delivery.deliver(job, bundle)
        items = self.bot.send_media_group.call_args.kwargs["media"]
        self.assertIsInstance(items[0], InputMediaPhoto)
        self.assertIsInstance(items[1], InputMediaVideo)
        self.assertEqual(self.database.file_id(123, job.cache_key, still.filename, "photo"), "photo-11")
        self.assertFalse(any(item.caption for item in items))

    async def test_successful_video_has_no_caption(self):
        await self.delivery.deliver(self.job(), self.bundle())
        self.assertNotIn("caption", self.bot.send_video.call_args.kwargs)

    async def test_group_button_is_shared_and_deduplicated(self):
        self.database.set_group(-123, True)
        job = self.job(chat_id=-123)
        self.database.complete_job(job.id)
        app = Application(self.settings, self.database, self.bot, None)
        for sender in (55, 66, 77):
            query = SimpleNamespace(
                data=f"m:{job.id}:audio",
                from_user=incoming(sender=sender).from_user,
                message=incoming(chat_id=-123),
                answer=AsyncMock(),
            )
            await media_action(query, app)
        self.assertEqual(self.database.claim().mode, "audio")
        self.assertIsNone(self.database.claim())

    async def test_group_mode_is_persisted_and_private_defaults_to_slideshow(self):
        self.database.set_group(-123, True)
        self.database.set_group_photo_mode(-123, "photos")
        self.database.set_group(-123, False)
        self.database.set_group(-123, True)
        self.assertEqual(self.database.group_photo_mode(-123), "photos")
        app = Application(self.settings, self.database, self.bot, None)
        group = await app.destination(incoming(chat_id=-123))
        app.submit(group, "https://youtu.be/abc")
        self.assertEqual(self.database.claim().mode, "album")

    def test_cache_birth_is_not_extended_and_is_capped_at_72_hours(self):
        bundle = self.bundle()
        with patch("storage.database.time.time", return_value=1000):
            self.database.put_cache("key", bundle, 7 * 86400)
        with patch("storage.database.time.time", return_value=2000):
            self.database.put_cache("key", bundle, 7 * 86400)
            self.database.get_cache("key")
        row = self.database.connection.execute("SELECT saved_at,expires_at FROM cache").fetchone()
        self.assertEqual(tuple(row), (1000, 1000 + 72 * 3600))

    async def test_refresh_does_not_remove_another_chats_leased_generation(self):
        pipeline = MediaPipeline(self.settings, self.database, None)
        old = self.bundle()
        generation = pipeline.cache / ("a" * 64 + "-" + "b" * 16)
        old.directory.rename(generation)
        old = replace(old, directory=generation)
        with pipeline.using(old):
            pipeline.maintain()
            self.assertTrue(old.directory.exists())
        pipeline.maintain()
        self.assertFalse(old.directory.exists())

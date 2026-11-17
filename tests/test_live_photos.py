import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import FSInputFile, InputMediaLivePhoto, InputMediaPhoto

from core.errors import MediaError
from core.models import Asset, MediaBundle
from services.delivery import TelegramDelivery
from services.gallery import GalleryAdapter
from services.processes import ProcessResult
from services.tiktok import PhotoPost, TikTokPhotos, parse_photo_page
from services.tiktok_live import parse_live_photos, public_live_photos
from tests.support import DatabaseFixture


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    identifier = "7000000000000000001"
    images = (
        ("https://p.tiktokcdn.com/still-one~original.jpeg",),
        ("https://p.tiktokcdn.com/still-two~original.jpeg",),
    )

    def response(self, **changes):
        data = {
            "id": self.identifier,
            "images": ["https://p.tiktokcdn.com/still-one~small.jpeg", "https://p.tiktokcdn.com/still-two~small.jpeg"],
            "live_images": [None, "https://v.tiktokcdn-us.com/motion.mp4"],
        }
        data.update(changes)
        return json.dumps({"code": 0, "data": data}).encode()

    def test_keeps_live_photo_in_second_position(self):
        result = parse_live_photos(self.response(), self.identifier, self.images)
        self.assertEqual(result, ((), ("https://v.tiktokcdn-us.com/motion.mp4",)))

    def test_wrong_post_or_still_order_is_rejected(self):
        for changes in (
            {"id": "7000000000000000002"},
            {
                "images": [
                    "https://p.tiktokcdn.com/still-two~small.jpeg",
                    "https://p.tiktokcdn.com/still-one~small.jpeg",
                ]
            },
            {"live_images": ["https://v.tiktokcdn-us.com/motion.mp4"]},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(parse_live_photos(self.response(**changes), self.identifier, self.images))

    def test_media_must_belong_to_tiktok_cdn(self):
        for url in (
            "http://v.tiktokcdn.com/clip",
            "https://127.0.0.1/clip",
            "https://tiktokcdn.com.evil.example/clip",
            "https://www.tikwm.com/clip",
            "https://user:secret@v.tiktokcdn.com/clip",
        ):
            with self.subTest(url=url):
                self.assertIsNone(
                    parse_live_photos(self.response(live_images=[None, url]), self.identifier, self.images)
                )

    async def test_lookup_sends_only_id_and_preserves_photos_on_timeout(self):
        http = SimpleNamespace(get_browser=AsyncMock(side_effect=TimeoutError()))
        self.assertIsNone(await public_live_photos(http, self.identifier, self.images))
        self.assertEqual(http.get_browser.call_args.args[0], f"https://www.tikwm.com/api/?url={self.identifier}&hd=1")
        http.get_browser.reset_mock()
        await public_live_photos(http, "invalid/?private=token", self.images)
        http.get_browser.assert_not_awaited()

    def test_private_state_is_not_eligible_for_lookup(self):
        post = {
            "id": self.identifier,
            "privateItem": True,
            "imagePost": {"images": [{"imageURL": {"urlList": self.images[0]}}]},
        }
        parsed = parse_photo_page('<script id="SIGI_STATE">' + json.dumps(post) + "</script>", self.identifier)
        self.assertFalse(parsed.is_public)

    def test_verified_post_without_motion_is_distinct_from_resolver_failure(self):
        self.assertEqual(parse_live_photos(self.response(live_images=[]), self.identifier, self.images), ((), ()))
        self.assertIsNone(parse_live_photos(b"not-json", self.identifier, self.images))


class MotionBundleTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def test_independent_gallery_fallback_also_discovers_live_photos(self):
        from services.links import normalize_url

        identifier = "7000000000000000001"
        metadata = {"id": identifier, "type": "image", "image": {}}
        payload = json.dumps(
            [[3, "https://p.tiktokcdn.com/one", metadata], [3, "https://p.tiktokcdn.com/two", metadata]]
        ).encode()
        proxy = AsyncMock()
        proxy.__aenter__.return_value = SimpleNamespace(url="http://guard")
        expected = self.bundle(photos=2)
        with (
            patch("services.gallery.SafeProxy", return_value=proxy),
            patch("services.gallery.run_process", new_callable=AsyncMock) as process,
            patch("services.gallery.TikTokPhotos.render_post", new_callable=AsyncMock) as render,
        ):
            process.return_value = ProcessResult(0, payload, b"")
            render.return_value = expected
            result = await GalleryAdapter(self.settings, None, None).download(
                normalize_url(f"https://www.tiktok.com/@user/photo/{identifier}"), self.root
            )
        self.assertIs(result, expected)
        post = render.call_args.args[2]
        self.assertEqual(post.identifier, identifier)
        self.assertEqual(len(post.images), 2)
        self.assertTrue(post.is_public)

    async def test_render_uses_motion_and_original_retains_native_pair(self):
        directory = self.root / "bundle"
        directory.mkdir()

        async def download(urls, target, limit):
            target.write_bytes(b"fixture")
            return "image/jpeg"

        async def compatible(source, directory):
            (directory / "video.mp4").write_bytes(b"clip")
            return Asset("video.mp4", "video", 3.4, 320, 240), True

        media = SimpleNamespace(
            probe=AsyncMock(return_value=SimpleNamespace(video_codec="mjpeg", width=320, height=240)),
            photo=AsyncMock(side_effect=lambda path, root: Asset(path.name, "photo")),
            compatible_video=AsyncMock(side_effect=compatible),
            extract_audio=AsyncMock(return_value=Asset("audio.m4a", "audio", 16)),
            slideshow=AsyncMock(return_value=(Asset("slideshow.mp4", "video", 6), True)),
        )
        photos = TikTokPhotos(self.settings, None, media)
        photos._download_candidates = AsyncMock(side_effect=download)
        post = PhotoPost(
            "Post",
            (("https://cdn.example/1",), ("https://cdn.example/2",)),
            ("https://cdn.example/music",),
            ((), ("https://v.tiktokcdn.com/motion",)),
        )
        from services.links import normalize_url

        result = await photos._render_post(normalize_url("https://www.tiktok.com/@user/photo/123"), directory, post)
        self.assertEqual(result.video.duration, 6)
        self.assertEqual([item.kind for item in result.originals], ["photo", "live_photo"])
        self.assertEqual(result.originals[1].photo_filename, "original-002.jpg")
        self.assertEqual(list(media.slideshow.call_args.kwargs["motion"]), [1])
        self.assertIn("original-002.jpg", {asset.filename for asset in result.assets})
        self.assertEqual(MediaBundle.deserialize(result.serialize(self.root), self.root), result)
        fallback = self.root / "fallback"
        fallback.mkdir()
        media.compatible_video = AsyncMock(side_effect=MediaError("temporary", code="network", retryable=True))
        degraded = await photos._render_post(normalize_url("https://www.tiktok.com/@user/photo/123"), fallback, post)
        self.assertEqual([a.kind for a in degraded.originals], ["photo", "photo"])
        self.assertTrue(degraded.motion_pending)

    async def test_private_or_disabled_lookup_never_contacts_resolver(self):
        from services.links import normalize_url

        for private, enabled in ((True, True), (False, False)):
            node = {
                "id": "7000000000000000001",
                "privateItem": private,
                "imagePost": {"images": [{"imageURL": {"urlList": ["https://p.tiktokcdn.com/still"]}}]},
            }
            page = '<script id="SIGI_STATE">' + json.dumps(node) + "</script>"
            http = SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        url="https://www.tiktok.com/@user/photo/7000000000000000001", body=page.encode()
                    )
                )
            )
            photos = TikTokPhotos(replace(self.settings, tiktok_live_photos=enabled), http, None)
            photos._render_post = AsyncMock(return_value=None)
            with patch("services.tiktok.public_live_photos", new_callable=AsyncMock) as lookup:
                await photos.download(
                    normalize_url("https://www.tiktok.com/@user/photo/7000000000000000001"), self.root
                )
                lookup.assert_not_awaited()


class NativeDeliveryTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.bundle_value = self.bundle(photos=2)
        live = replace(
            self.bundle_value.video, kind="live_photo", photo_filename=self.bundle_value.originals[1].filename
        )
        still = replace(self.bundle_value.originals[0], kind="photo")
        self.bundle_value = replace(self.bundle_value, video=None, originals=(still, live))
        photo = SimpleNamespace(message_id=11, photo=[SimpleNamespace(file_id="still-id")])
        live_message = SimpleNamespace(
            message_id=12, live_photo=SimpleNamespace(file_id="live-id", photo=[SimpleNamespace(file_id="cover-id")])
        )
        self.bot = SimpleNamespace(
            id=123,
            send_media_group=AsyncMock(return_value=[photo, live_message]),
            send_live_photo=AsyncMock(return_value=live_message),
        )
        self.delivery = TelegramDelivery(self.bot, self.database, self.settings)

    async def test_native_album_and_both_cached_components_survive_disk_eviction(self):
        job = self.job(mode="photos")
        await self.delivery.deliver(job, self.bundle_value)
        media = self.bot.send_media_group.call_args.kwargs["media"]
        self.assertIsInstance(media[0], InputMediaPhoto)
        self.assertIsInstance(media[1], InputMediaLivePhoto)
        self.assertIsInstance(media[1].photo, FSInputFile)
        self.database.complete_job(job.id)
        for asset in self.bundle_value.assets:
            asset.path(self.bundle_value.directory).unlink()
        next_job = self.job(mode="photos", message_id=2)
        cached = self.delivery.cached(next_job)
        self.assertIsNotNone(cached)
        await self.delivery.deliver(next_job, cached)
        media = self.bot.send_media_group.call_args.kwargs["media"]
        self.assertEqual(media[1].media, "live-id")
        self.assertEqual(media[1].photo, "cover-id")

    async def test_cache_requires_cover_id_too(self):
        job = self.job(mode="photos")
        await self.delivery.deliver(job, self.bundle_value)
        cover = self.bundle_value.originals[1].photo_filename
        self.database.forget_file_id(123, job.cache_key, cover, "photo")
        self.assertIsNone(self.delivery.cached(job))

    async def test_degraded_result_is_not_reused_through_telegram_file_ids(self):
        job = self.job(mode="photos")
        await self.delivery.deliver(job, replace(self.bundle_value, motion_pending=True))
        self.assertIsNone(self.delivery.cached(job))

    async def test_single_native_live_photo_uses_the_paired_method(self):
        job = self.job(mode="photos")
        bundle = replace(self.bundle_value, originals=(self.bundle_value.originals[1],))
        await self.delivery.deliver(job, bundle)
        self.bot.send_live_photo.assert_awaited_once()
        self.assertIn("photo", self.bot.send_live_photo.call_args.kwargs)

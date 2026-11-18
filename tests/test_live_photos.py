import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo

from core.errors import MediaError
from core.models import Asset, MediaBundle
from services.delivery import TelegramDelivery
from services.gallery import GalleryAdapter
from services.processes import ProcessResult
from services.tiktok import PhotoPost, TikTokPhotos, parse_photo_page
from services.tiktok_live import parse_live_photos, public_live_photos, public_video_urls
from tests.support import DatabaseFixture


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    identifier = "7000000000000000001"
    images = (
        ("https://p.tiktokcdn.com/still-one~original.jpeg",),
        ("https://p.tiktokcdn.com/still-two~original.jpeg",),
    )

    def response(self, **changes):
        data = {
            "id_str": self.identifier,
            "image_post_info": {
                "images": [
                    {"display_image": {"url_list": [self.images[0][0]]}},
                    {
                        "display_image": {"url_list": [self.images[1][0]]},
                        "video": {"url_list": ["https://v.tiktokcdn-us.com/motion.mp4"]},
                    },
                ]
            },
        }
        data.update(changes)
        return json.dumps({"status_code": 0, "items": [data]}).encode()

    def test_keeps_live_photo_in_second_position(self):
        self.assertEqual(
            parse_live_photos(self.response(), self.identifier, self.images),
            ((), ("https://v.tiktokcdn-us.com/motion.mp4",)),
        )

    def test_wrong_post_or_still_order_is_rejected(self):
        self.assertIsNone(parse_live_photos(self.response(id_str="7000000000000000002"), self.identifier, self.images))
        self.assertIsNone(parse_live_photos(self.response(), self.identifier, self.images[::-1]))

    def test_media_must_belong_to_tiktok(self):
        for url in (
            "http://v.tiktokcdn.com/clip",
            "https://127.0.0.1/clip",
            "https://tiktokcdn.com.evil.example/clip",
            "https://resolver.example/clip",
            "https://user:secret@v.tiktokcdn.com/clip",
        ):
            body = json.loads(self.response())
            body["items"][0]["image_post_info"]["images"][1]["video"]["url_list"] = [url]
            self.assertIsNone(parse_live_photos(json.dumps(body).encode(), self.identifier, self.images))

    async def test_lookup_contacts_only_first_party_and_preserves_photos_on_timeout(self):
        http = SimpleNamespace(get_browser=AsyncMock(side_effect=TimeoutError()))
        self.assertIsNone(await public_live_photos(http, self.identifier, self.images))
        self.assertEqual(
            http.get_browser.call_args.args[0],
            f"https://www.tiktok.com/player/api/v1/items?item_ids={self.identifier}&aid=1988",
        )
        http.get_browser.reset_mock()
        await public_live_photos(http, "invalid/?private=token", self.images)
        http.get_browser.assert_not_awaited()

    def test_absent_motion_remains_unknown_rather_than_asserting_static(self):
        body = json.loads(self.response())
        del body["items"][0]["image_post_info"]["images"][1]["video"]
        self.assertIsNone(parse_live_photos(json.dumps(body).encode(), self.identifier, self.images))
        self.assertIsNone(parse_live_photos(b"not-json", self.identifier, self.images))

    def test_private_video_page_is_not_eligible_for_public_fallback(self):
        from services.tiktok import parse_video_page

        node = {"id": self.identifier, "privateItem": True, "video": {"playAddr": "https://cdn.example/clip"}}
        self.assertIsNone(
            parse_video_page('<script id="SIGI_STATE">' + json.dumps(node) + "</script>", self.identifier)
        )

    def test_private_photo_state_is_not_eligible_for_lookup(self):
        node = {
            "id": self.identifier,
            "privateItem": True,
            "imagePost": {"images": [{"imageURL": {"urlList": self.images[0]}}]},
        }
        self.assertFalse(
            parse_photo_page('<script id="SIGI_STATE">' + json.dumps(node) + "</script>", self.identifier).is_public
        )

    async def test_player_prefers_h264_and_preserves_signed_resource(self):
        from urllib.parse import urlsplit

        signed = "https://www.tiktok.com/aweme/v1/play/?video_id=clip&file_id=file&sign=unchanged"
        body = {
            "status_code": 0,
            "items": [
                {
                    "id_str": self.identifier,
                    "video_info": {
                        "profiles": [
                            {
                                "codec_type": "h265",
                                "bitrate": 900,
                                "play_addr": {"url_list": ["https://v.tiktokcdn.com/hevc"]},
                            },
                            {"codec_type": "h264", "bitrate": 400, "play_addr": {"url_list": [signed]}},
                        ]
                    },
                }
            ],
        }
        http = SimpleNamespace(get_browser=AsyncMock(return_value=SimpleNamespace(body=json.dumps(body).encode())))
        urls = await public_video_urls(http, self.identifier)
        self.assertEqual(urlsplit(urls[0]).hostname, "api16-normal-useast8.tiktokv.us")
        self.assertEqual(urlsplit(urls[0]).query, urlsplit(signed).query)
        body["items"][0]["id_str"] = "7000000000000000002"
        http.get_browser.return_value.body = json.dumps(body).encode()
        self.assertEqual(await public_video_urls(http, self.identifier), ())


class DirectVideoTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def test_conversion_timeout_never_downloads_and_encodes_each_alternate_again(self):
        from services.links import normalize_url

        node = {
            "id": "7000000000000000001",
            "video": {"playAddr": ["https://v.tiktokcdn.com/one.mp4", "https://v.tiktokcdn.com/two.mp4"]},
        }
        http = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    url="https://www.tiktok.com/@user/video/7000000000000000001",
                    body=('<script id="SIGI_STATE">' + json.dumps(node) + "</script>").encode(),
                )
            ),
            get_browser=AsyncMock(return_value=SimpleNamespace(body=b'{"status_code":1}')),
        )
        media = SimpleNamespace(
            probe=AsyncMock(return_value=SimpleNamespace(video_codec="h264")),
            compatible_video=AsyncMock(side_effect=MediaError("slow encode", code="timeout", retryable=True)),
        )
        photos = TikTokPhotos(self.settings, http, media)
        photos._download_candidates = AsyncMock()
        with self.assertRaises(MediaError):
            await photos.download(normalize_url(http.get.return_value.url), self.root)
        photos._download_candidates.assert_awaited_once()
        media.compatible_video.assert_awaited_once()


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

        async def slideshow(images, audio, directory, **kwargs):
            (directory / "slideshow.mp4").write_bytes(b"moving" if kwargs.get("motion") else b"static")
            return Asset("slideshow.mp4", "video", 6), True

        media = SimpleNamespace(
            probe=AsyncMock(return_value=SimpleNamespace(video_codec="mjpeg", width=320, height=240)),
            photo=AsyncMock(side_effect=lambda path, root: Asset(path.name, "photo")),
            compatible_video=AsyncMock(side_effect=compatible),
            extract_audio=AsyncMock(return_value=Asset("audio.m4a", "audio", 16)),
            slideshow=AsyncMock(side_effect=slideshow),
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
        self.assertNotEqual(result.video.filename, degraded.video.filename)

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


class MotionDeliveryTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.bundle_value = self.bundle(photos=2)
        live = replace(
            self.bundle_value.video, kind="live_photo", photo_filename=self.bundle_value.originals[1].filename
        )
        still = replace(self.bundle_value.originals[0], kind="photo")
        self.bundle_value = replace(self.bundle_value, video=None, originals=(still, live))
        photo = SimpleNamespace(message_id=11, photo=[SimpleNamespace(file_id="still-id")])
        live_message = SimpleNamespace(message_id=12, video=SimpleNamespace(file_id="motion-video-id"))
        self.bot = SimpleNamespace(
            id=123,
            send_media_group=AsyncMock(return_value=[photo, live_message]),
            send_video=AsyncMock(return_value=live_message),
        )
        self.delivery = TelegramDelivery(self.bot, self.database, self.settings)

    async def test_album_motion_is_playable_video_and_survives_disk_eviction(self):
        job = self.job(mode="photos")
        await self.delivery.deliver(job, self.bundle_value)
        media = self.bot.send_media_group.call_args.kwargs["media"]
        self.assertIsInstance(media[0], InputMediaPhoto)
        self.assertIsInstance(media[1], InputMediaVideo)
        self.assertIsInstance(media[1].media, FSInputFile)
        self.assertTrue(media[1].supports_streaming)
        self.database.complete_job(job.id)
        for asset in self.bundle_value.assets:
            asset.path(self.bundle_value.directory).unlink()
        next_job = self.job(mode="photos", message_id=2)
        cached = self.delivery.cached(next_job)
        self.assertIsNotNone(cached)
        await self.delivery.deliver(next_job, cached)
        media = self.bot.send_media_group.call_args.kwargs["media"]
        self.assertEqual(media[1].media, "motion-video-id")

    async def test_legacy_native_ids_cannot_return_a_static_looking_cached_album(self):
        job = self.job(mode="photos")
        still, live = self.bundle_value.originals
        self.database.remember_delivery(job.cache_key, self.bundle_value)
        self.database.save_file_id(123, job.cache_key, still.filename, "photo", "still-id")
        self.database.save_file_id(123, job.cache_key, live.filename, "live_photo", "old-native-id")
        self.database.save_file_id(123, job.cache_key, live.photo_filename, "photo", "old-cover-id")
        self.assertIsNone(self.delivery.cached(job))
        await self.delivery.deliver(job, self.bundle_value)
        self.assertIsInstance(self.bot.send_media_group.call_args.kwargs["media"][1].media, FSInputFile)

    async def test_degraded_result_is_not_reused_through_telegram_file_ids(self):
        job = self.job(mode="photos")
        await self.delivery.deliver(job, replace(self.bundle_value, motion_pending=True))
        self.assertIsNone(self.delivery.cached(job))

    async def test_single_live_photo_uses_full_video_in_every_visual_mode(self):
        live = self.bundle_value.originals[1]
        bundle = replace(self.bundle_value, originals=(live,))
        for mode in ("photos", "album", "video"):
            item = bundle.items(mode)[0]
            self.assertEqual(item.kind, "video")
            self.assertEqual(item.duration, live.duration)
            self.assertEqual(item.filename, live.filename)
            self.assertFalse(item.photo_filename)
        self.assertEqual(bundle.originals[0].kind, "live_photo")
        job = self.job(mode="photos")
        await self.delivery.deliver(job, bundle)
        self.bot.send_video.assert_awaited_once()
        self.assertEqual(self.bot.send_video.call_args.kwargs["duration"], int(live.duration))

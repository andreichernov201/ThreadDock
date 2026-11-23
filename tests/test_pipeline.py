import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from core.errors import MediaError
from core.models import Asset, RenderOptions
from services.links import normalize_url
from services.pipeline import KeyedLocks, MediaPipeline
from tests.support import DatabaseFixture


class PipelineTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.pipeline = MediaPipeline(self.settings, self.database, None)
        self.pipeline.gallery.download = AsyncMock(side_effect=MediaError("Unavailable", code="unavailable"))
        self.pipeline.video.download = AsyncMock(side_effect=self.download)
        self.pipeline.tools.compatible_video = AsyncMock(side_effect=self.convert)
        self.pipeline.tools.extract_audio = AsyncMock(return_value=None)
        self.pipeline.instagram.entries = AsyncMock(side_effect=MediaError("missing", code="instagram_embed_missing"))
        self.pipeline.comments.resolve = AsyncMock(side_effect=lambda link: link)
        self.pipeline.photos.download = AsyncMock(return_value=None)

    async def download(self, link, directory):
        path = directory / "source.mp4"
        path.write_bytes(b"source")
        (directory / "source.info.json").write_text('{"secret":"signed-url"}')
        return path, "Example"

    async def test_live_rejection_does_not_run_any_downloader(self):
        for url in ["https://www.tiktok.com/@one/live", "https://www.instagram.com/one/live/123"]:
            with self.assertRaises(MediaError) as caught:
                await self.pipeline.fetch(url)
            self.assertEqual(caught.exception.code, "live_unsupported")
        self.pipeline.video.download.assert_not_awaited()
        self.pipeline.gallery.download.assert_not_awaited()

    async def test_metadata_live_rejection_does_not_try_gallery(self):
        self.pipeline.video.download.side_effect = MediaError("Live", code="live_unsupported")
        with self.assertRaises(MediaError):
            await self.pipeline.fetch("https://www.tiktok.com/@one/video/123")
        self.pipeline.gallery.download.assert_not_awaited()

    async def test_comments_never_share_captions_with_other_requests(self):
        base = "https://www.tiktok.com/@one/video/123"
        self.pipeline.comments.text = AsyncMock(side_effect=lambda link: "Comment " + link.comment_id)
        first = await self.pipeline.fetch(base + "?share_comment_id=12")
        second = await self.pipeline.fetch(base + "?share_comment_id=13")
        ordinary = await self.pipeline.fetch(base)
        self.assertEqual(first.comment, "Comment 12")
        self.assertEqual(second.comment, "Comment 13")
        self.assertEqual(ordinary.comment, "")
        self.assertNotEqual(first.directory, second.directory)
        self.assertEqual(self.pipeline.video.download.await_count, 3)
        self.assertEqual((await self.pipeline.fetch(base + "?comment_id=12")).comment, "Comment 12")
        self.assertEqual(self.pipeline.video.download.await_count, 3)

    async def test_comment_lookup_starts_during_video_preparation(self):
        started = asyncio.Event()

        async def comment(link):
            started.set()
            return "Selected"

        async def video(link, directory):
            await asyncio.wait_for(started.wait(), 1)
            return await self.download(link, directory)

        self.pipeline.comments.text = AsyncMock(side_effect=comment)
        self.pipeline.video.download.side_effect = video
        bundle = await self.pipeline.fetch("https://www.tiktok.com/@one/video/123?comment_id=12")
        self.assertEqual(bundle.comment, "Selected")

    async def test_missing_comment_still_returns_the_video(self):
        self.pipeline.comments.text = AsyncMock(return_value="")
        bundle = await self.pipeline.fetch("https://www.tiktok.com/@one/video/123?comment_id=12")
        self.assertIsNotNone(bundle.video)
        self.assertEqual(bundle.comment, "")

    async def test_short_comment_cache_keeps_the_active_job_key_after_redirect(self):
        short = "https://vt.tiktok.com/abc/"
        resolved = normalize_url("https://www.tiktok.com/@one/video/123?share_comment_id=12")
        self.pipeline.comments.resolve.return_value = resolved
        self.pipeline.comments.resolve.side_effect = None
        self.pipeline.comments.text = AsyncMock(return_value="Selected")
        job = self.job(url=short)
        bundle = await self.pipeline.fetch(short)
        self.assertEqual(self.database.get_cache(job.cache_key).comment, "Selected")
        self.pipeline.photos.download.assert_awaited_once()
        self.database.connection.execute("UPDATE cache SET expires_at=0")
        self.database.connection.commit()
        self.pipeline.maintain()
        self.assertTrue(bundle.directory.is_dir())

    async def test_failed_video_cancels_optional_comment_work(self):
        ended = asyncio.Event()

        async def lookup(link):
            try:
                await asyncio.Future()
            finally:
                ended.set()

        async def failure(link, directory):
            await asyncio.sleep(0)
            raise MediaError("Too large", code="file_too_large")

        self.pipeline.comments.text = AsyncMock(side_effect=lookup)
        self.pipeline.video.download.side_effect = failure
        with self.assertRaises(MediaError):
            await self.pipeline.fetch("https://www.tiktok.com/@one/video/123?comment_id=12")
        self.assertTrue(ended.is_set())
        self.assertEqual(list(self.pipeline.work.iterdir()), [])

    async def convert(self, source, directory):
        (directory / "video.mp4").write_bytes(b"processed")
        return Asset("video.mp4", "video", 1, 320, 240), True

    async def test_confirmed_photo_error_never_reaches_video_extractor(self):
        from services.tiktok import PhotoModeError

        self.pipeline.photos.download = AsyncMock(
            side_effect=PhotoModeError("Photo metadata unavailable", code="photo_metadata_missing")
        )
        with self.assertRaises(PhotoModeError):
            await self.pipeline.fetch("https://www.tiktok.com/@someone/photo/123")
        self.pipeline.video.download.assert_not_awaited()

    async def test_direct_size_failure_does_not_restart_other_extractors(self):
        self.pipeline.photos.download = AsyncMock(side_effect=MediaError("Too large", code="file_too_large"))
        with self.assertRaises(MediaError):
            await self.pipeline.fetch("https://vt.tiktok.com/abc/")
        self.pipeline.video.download.assert_not_awaited()
        self.pipeline.gallery.download.assert_not_awaited()

    async def test_video_duration_failure_does_not_restart_photo_or_gallery_extractors(self):
        self.pipeline.photos.download = AsyncMock()
        self.pipeline.video.download.side_effect = MediaError("Too long", code="duration_limit")
        with self.assertRaises(MediaError):
            await self.pipeline.fetch("https://www.tiktok.com/@user/video/123")
        self.pipeline.photos.download.assert_not_awaited()
        self.pipeline.gallery.download.assert_not_awaited()

    async def test_short_tiktok_link_preserves_photo_failure_on_video_fallback(self):
        from core.errors import MediaError

        self.pipeline.photos.download = AsyncMock(
            side_effect=MediaError("Source rejected photo", code="source_http", retryable=True)
        )
        self.pipeline.video.download.side_effect = MediaError("Unsupported URL", code="extractor_failed")
        with self.assertRaises(MediaError) as captured:
            await self.pipeline.fetch("https://vm.tiktok.com/abc/")
        self.assertEqual(captured.exception.code, "source_http")
        self.assertTrue(captured.exception.retryable)

    async def test_video_route_can_use_photo_fallback(self):
        from core.errors import MediaError
        from core.models import MediaBundle

        async def photo_download(link, directory):
            (directory / "video.mp4").write_bytes(b"fixture-video")
            (directory / "original.jpg").write_bytes(b"fixture-image")
            return MediaBundle(
                directory,
                link.url,
                link.platform,
                "Photo",
                Asset("video.mp4", "video", 1, 320, 240),
                (Asset("original.jpg", "document"),),
            )

        self.pipeline.video.download.side_effect = MediaError("Unsupported URL", code="extractor_failed")
        self.pipeline.photos.download = AsyncMock(side_effect=photo_download)
        bundle = await self.pipeline.fetch("https://www.tiktok.com/@someone/video/123")
        self.assertEqual(len(bundle.originals), 1)
        self.pipeline.photos.download.assert_awaited_once()

    async def test_download_publishes_only_result_assets(self):
        bundle = await self.pipeline.fetch("https://youtu.be/abc")
        self.assertEqual([path.name for path in bundle.directory.iterdir()], ["video.mp4"])
        self.assertEqual(list(self.pipeline.work.iterdir()), [])

    async def test_instagram_reel_uses_video_without_waiting_for_gallery(self):
        await self.pipeline.fetch("https://www.instagram.com/reel/example/")
        self.pipeline.video.download.assert_awaited_once()
        self.pipeline.gallery.download.assert_not_awaited()

    async def test_instagram_embed_success_skips_both_cli_extractors(self):
        from core.models import MediaBundle

        async def render(link, directory, entries, **kwargs):
            (directory / "native.mp4").write_bytes(b"fixture")
            return MediaBundle(directory, link.url, link.platform, "Video", Asset("native.mp4", "video"))

        self.pipeline.instagram.entries.side_effect = None
        self.pipeline.instagram.entries.return_value = [("https://cdninstagram.com/video.mp4", {"type": "video"})]
        self.pipeline.gallery.render_entries = AsyncMock(side_effect=render)
        await self.pipeline.fetch("https://www.instagram.com/reel/example/")
        self.pipeline.video.download.assert_not_awaited()
        self.pipeline.gallery.download.assert_not_awaited()
        self.assertTrue(self.pipeline.gallery.render_entries.call_args.kwargs["extract_audio"])

    async def test_known_instagram_carousel_cannot_fall_back_to_partial_video(self):
        from services.instagram import InstagramGalleryError

        self.pipeline.instagram.entries.side_effect = InstagramGalleryError(
            "incomplete", code="instagram_embed_missing"
        )
        with self.assertRaises(MediaError):
            await self.pipeline.fetch("https://www.instagram.com/reel/example/")
        self.pipeline.gallery.download.assert_awaited_once()
        self.pipeline.video.download.assert_not_awaited()

    async def test_instagram_reel_can_fall_back_to_gallery(self):
        from core.models import MediaBundle

        async def gallery(link, directory):
            (directory / "video.mp4").write_bytes(b"fixture")
            return MediaBundle(directory, link.url, link.platform, "Reel", Asset("video.mp4", "video"))

        self.pipeline.video.download.side_effect = MediaError("not available", code="extractor_failed")
        self.pipeline.gallery.download.side_effect = gallery
        bundle = await self.pipeline.fetch("https://www.instagram.com/reel/example/")
        self.assertEqual(bundle.video.filename, "video.mp4")
        self.pipeline.gallery.download.assert_awaited_once()

    async def test_instagram_photo_posts_still_try_gallery_first(self):
        from core.models import MediaBundle

        async def gallery(link, directory):
            (directory / "photo.jpg").write_bytes(b"fixture")
            return MediaBundle(
                directory, link.url, link.platform, "Photo", None, originals=(Asset("photo.jpg", "photo"),)
            )

        self.pipeline.gallery.download.side_effect = gallery
        bundle = await self.pipeline.fetch("https://www.instagram.com/p/example/")
        self.assertEqual(len(bundle.originals), 1)
        self.pipeline.video.download.assert_not_awaited()

    async def test_repeated_link_reuses_cache(self):
        first = await self.pipeline.fetch("https://youtu.be/abc?si=one")
        second = await self.pipeline.fetch("https://youtu.be/abc?si=two")
        self.assertEqual(first, second)
        self.pipeline.video.download.assert_awaited_once()

    async def test_custom_render_has_own_cache_and_does_not_change_shared_tools(self):
        url = "https://youtu.be/abc"
        base = normalize_url(url).cache_key
        render = RenderOptions(5, 10)
        with patch("services.pipeline.MediaTools", return_value=self.pipeline.tools) as constructor:
            custom = await self.pipeline.fetch(url, render=render)
        self.assertEqual(constructor.call_args.args[0].slideshow_seconds, 5)
        self.assertEqual(constructor.call_args.args[0].live_photo_limit, 10)
        default = await self.pipeline.fetch(url)
        self.assertNotEqual(default.directory, custom.directory)
        self.assertEqual(self.database.get_cache(base), default)
        self.assertEqual(self.database.get_cache(render.cache_key(base)), custom)
        self.assertEqual(self.pipeline.settings.slideshow_seconds, 3)

    async def test_missing_motion_is_refetched_instead_of_freezing_a_partial_result(self):
        first = await self.pipeline.fetch("https://youtu.be/abc")
        key = normalize_url(first.source_url).cache_key
        self.database.put_cache(key, replace(first, motion_pending=True), 60)
        second = await self.pipeline.fetch("https://youtu.be/abc")
        self.assertEqual(self.pipeline.video.download.await_count, 2)
        self.assertNotEqual(first.directory, second.directory)

    async def test_concurrent_identical_links_download_once(self):
        first, second = await asyncio.gather(
            self.pipeline.fetch("https://youtu.be/abc"), self.pipeline.fetch("https://youtu.be/abc")
        )
        self.assertEqual(first, second)
        self.pipeline.video.download.assert_awaited_once()
        self.assertEqual(self.pipeline.locks._entries, {})

    async def test_failed_download_cleans_workspace(self):
        self.pipeline.video.download.side_effect = OSError("private-url-must-not-be-shown")
        with self.assertRaisesRegex(Exception, "сохранить") as captured:
            await self.pipeline.fetch("https://youtu.be/abc")
        self.assertNotIn("private-url", str(captured.exception))
        self.assertEqual(list(self.pipeline.work.iterdir()), [])

    async def test_expired_cache_is_removed(self):
        bundle = await self.pipeline.fetch("https://youtu.be/abc")
        self.database.connection.execute("UPDATE cache SET expires_at=0")
        self.database.connection.commit()
        self.pipeline.maintain()
        self.assertFalse(bundle.directory.exists())

    async def test_active_job_pins_cache_during_maintenance(self):
        bundle = await self.pipeline.fetch("https://youtu.be/abc")
        self.job()
        self.database.connection.execute("UPDATE cache SET expires_at=0")
        self.database.connection.commit()
        self.pipeline.maintain()
        self.assertTrue(bundle.directory.exists())

    async def test_orphan_after_interrupted_publish_is_cleaned(self):
        orphan = self.pipeline.cache / ("a" * 64)
        orphan.mkdir()
        (orphan / "video.mp4").write_bytes(b"orphan")
        self.pipeline.maintain()
        self.assertFalse(orphan.exists())

    async def test_oversized_optional_audio_does_not_discard_video(self):
        from core.errors import MediaError

        self.pipeline.tools.extract_audio.side_effect = MediaError("Too large", code="file_too_large")
        bundle = await self.pipeline.fetch("https://youtu.be/abc")
        self.assertTrue((bundle.directory / bundle.video.filename).is_file())
        self.assertIsNone(bundle.audio)

    async def test_optional_audio_conversion_failure_keeps_video(self):
        from core.errors import MediaError

        self.pipeline.tools.extract_audio.side_effect = MediaError("Conversion failed", code="conversion_failed")
        bundle = await self.pipeline.fetch("https://youtu.be/abc")
        self.assertTrue((bundle.directory / bundle.video.filename).is_file())
        self.assertIsNone(bundle.audio)


class LockTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_waiter_does_not_leak_lock(self):
        locks = KeyedLocks()
        async with locks.hold("key"):

            async def waiting():
                async with locks.hold("key"):
                    pass

            task = asyncio.create_task(waiting())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(locks._entries, {})

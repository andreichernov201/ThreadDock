"""TikTok Photo Mode from public page state; no third-party downloader APIs."""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from config import Settings
from core import texts as T
from core.errors import MediaError
from core.models import Asset, MediaBundle
from services.http import PublicHttp
from services.links import VideoLink
from services.media import MediaTools
from services.processes import directory_size

logger = logging.getLogger(__name__)


class PhotoModeError(MediaError):
    """The page is a confirmed photo post; do not fall back to a video extractor."""


@dataclass(frozen=True)
class PhotoPost:
    title: str
    images: tuple[tuple[str, ...], ...]
    music: tuple[str, ...]
    motion: tuple[tuple[str, ...], ...] = ()
    story: bool = False


class _PageState(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.current: list[str] = []
        self.documents: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") in {
            "__UNIVERSAL_DATA_FOR_REHYDRATION__",
            "SIGI_STATE",
            "sigi-persisted-data",
        }:
            self.active = True
            self.current = []

    def handle_data(self, data):
        if self.active:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.documents.append("".join(self.current))
            self.active = False


def _urls(value) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, dict):
        return _urls(value.get("urlList") or value.get("UrlList") or value.get("url_list") or value.get("url") or [])
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _motion_urls(image: dict) -> tuple[str, ...]:
    video = image.get("video") or image.get("livePhoto") or image.get("live_photo") or {}
    if isinstance(video, dict):
        video = video.get("playAddr") or video.get("play_addr") or video.get("videoURL") or video
    return _urls(image.get("videoURL") or image.get("video_url") or image.get("livePhotoVideo") or video)


def parse_photo_page(html: str, expected_id: str | None = None) -> PhotoPost | None:
    parser = _PageState()
    parser.feed(html)
    for document in parser.documents:
        try:
            stack = [json.loads(document)]
        except ValueError:
            continue
        visited = 0
        while stack and visited < 50000:
            node = stack.pop()
            visited += 1
            if isinstance(node, dict):
                identifier = str(node.get("id") or node.get("aweme_id") or "")
                image_post = node.get("imagePost") or node.get("image_post_info") or {}
                images = image_post.get("images", []) if isinstance(image_post, dict) else []
                if images and (expected_id is None or identifier == expected_id):
                    if not isinstance(images, list) or not all(isinstance(image, dict) for image in images):
                        raise MediaError(T.SERVICES_TIKTOK_INVALID_PHOTO_DATA, code="photo_metadata")
                    candidates = tuple(_urls(image.get("imageURL") or image.get("display_image")) for image in images)
                    if any(not item for item in candidates):
                        raise MediaError(T.SERVICES_TIKTOK_INCOMPLETE_PHOTO_DATA, code="photo_metadata")
                    music = node.get("music") or {}
                    if not isinstance(music, dict):
                        music = {}
                    return PhotoPost(
                        str(node.get("desc") or "TikTok Photo Mode")[:500],
                        candidates,
                        _urls(music.get("playUrl") or music.get("play_url")),
                        tuple(_motion_urls(image) for image in images),
                        bool(node.get("story") or node.get("is_story")),
                    )
                stack.extend(reversed(list(node.values())))
            elif isinstance(node, list):
                stack.extend(reversed(node))
    return None


def parse_video_page(html: str, expected_id: str | None = None) -> tuple[str, tuple[str, ...]] | None:
    parser = _PageState()
    parser.feed(html)
    for document in parser.documents:
        try:
            stack = [json.loads(document)]
        except ValueError:
            continue
        visited = 0
        while stack and visited < 50000:
            node = stack.pop()
            visited += 1
            if isinstance(node, dict):
                identifier = str(node.get("id") or "")
                video = node.get("video")
                if (
                    identifier
                    and (expected_id is None or identifier == expected_id)
                    and isinstance(video, dict)
                    and not node.get("imagePost")
                ):
                    urls = []
                    variants = video.get("bitrateInfo") or []
                    if isinstance(variants, list):
                        variants = [v for v in variants if isinstance(v, dict)]
                        for variant in sorted(variants, key=lambda v: int(v.get("Bitrate") or 0), reverse=True):
                            if "dash" not in str(variant.get("Format", "")).lower():
                                urls.extend(_urls(variant.get("PlayAddr")))
                    urls.extend(_urls(video.get("playAddr")))
                    urls.extend(_urls(video.get("PlayAddrStruct")))
                    if urls:
                        return str(node.get("desc") or "TikTok")[:500], tuple(dict.fromkeys(urls))
                stack.extend(reversed(list(node.values())))
            elif isinstance(node, list):
                stack.extend(reversed(node))
    return None


class TikTokPhotos:
    def __init__(self, settings: Settings, http: PublicHttp, media: MediaTools):
        self.settings, self.http, self.media = settings, http, media

    async def _download_candidates(self, urls: tuple[str, ...], target: Path, limit: int) -> str:
        last_error = None
        for url in urls[:5]:
            try:
                # CDN addresses sometimes use an HTTP spelling but also support HTTPS.
                if url.startswith("http://"):
                    url = "https://" + url[7:]
                return (await self.http.get(url, limit, destination=target)).content_type
            except MediaError as error:
                last_error = error
        raise last_error or MediaError(T.SERVICES_TIKTOK_MEDIA_URL_MISSING, code="photo_metadata")

    async def download(self, link: VideoLink, directory: Path) -> MediaBundle | None:
        response = await self.http.get(link.url, 8 * 1024**2)
        match = re.search(r"/(?:photo|video|story|stories)/(\d+)", response.url)
        confirmed_photo = "/photo/" in link.url or "/photo/" in response.url
        try:
            post = parse_photo_page(response.body.decode("utf-8", errors="replace"), match[1] if match else None)
            if post is None and match and "/photo/" in response.url:
                # The public photo route can omit itemStruct while the canonical
                # video spelling exposes the same public post's complete state.
                canonical = re.sub(r"/photo/(\d+)", r"/video/\1", response.url)
                alternate = await self.http.get(canonical, 8 * 1024**2)
                post = parse_photo_page(alternate.body.decode("utf-8", errors="replace"), match[1])
        except MediaError as error:
            raise PhotoModeError(str(error), code=error.code, retryable=error.retryable) from error
        logger.info(
            "tiktok_photo_probe route=%s metadata=%s bytes=%s",
            "photo" if confirmed_photo else "video" if "/video/" in response.url else "other",
            "found" if post is not None else "missing",
            len(response.body),
        )
        if post is None:
            if confirmed_photo:
                raise PhotoModeError(T.SERVICES_TIKTOK_PHOTO_METADATA_UNAVAILABLE, code="photo_metadata_missing")
            video = parse_video_page(response.body.decode("utf-8", errors="replace"), match[1] if match else None)
            if video is None:
                return None
            title, urls = video
            source = directory / "tiktok-source.mp4"
            await self._download_candidates(urls, source, self.settings.max_download_bytes)
            asset, has_audio = await self.media.compatible_video(source, directory)
            try:
                async with asyncio.timeout(min(30, self.settings.process_timeout)):
                    audio = await self.media.extract_audio(source, directory)
            except (MediaError, TimeoutError) as error:
                logger.info("optional_tiktok_audio code=%s", getattr(error, "code", "timeout"))
                audio = None
            return MediaBundle(directory, link.url, link.platform, title, asset, audio=audio, has_audio=has_audio)
        try:
            return await self._render_post(link, directory, post)
        except MediaError as error:
            raise PhotoModeError(str(error), code=error.code, retryable=error.retryable) from error

    async def _render_post(self, link: VideoLink, directory: Path, post: PhotoPost) -> MediaBundle:
        if len(post.images) > self.settings.max_photos:
            raise MediaError(T.SERVICES_TIKTOK_TOO_MANY_PHOTOS, code="photo_count")
        images: list[Path] = []
        for index, urls in enumerate(post.images):
            original = directory / f"original-{index + 1:03}.img"
            content_type = await self._download_candidates(
                urls, original, min(30 * 1024**2, self.settings.max_upload_bytes)
            )
            extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/avif": ".avif"}.get(
                content_type, ".img"
            )
            original = original.rename(original.with_suffix(extension))
            info = await self.media.probe(original)
            if not info.video_codec or info.width * info.height > 80_000_000:
                raise MediaError(T.SERVICES_TIKTOK_INVALID_IMAGE, code="invalid_image")
            photo = await self.media.photo(original, directory)
            images.append(photo.path(directory))
            if directory_size(directory) > self.settings.max_download_bytes:
                raise MediaError(T.SERVICES_TIKTOK_PHOTO_POST_TOO_LARGE, code="file_too_large")
        music = None
        audio_asset = None
        if post.music:
            try:
                async with asyncio.timeout(min(30, self.settings.process_timeout)):
                    music = directory / "music.bin"
                    await self._download_candidates(
                        post.music, music, min(self.settings.max_download_bytes, self.settings.max_upload_bytes)
                    )
                    audio_asset = await self.media.extract_audio(music, directory)
                    music = audio_asset.path(directory) if audio_asset else None
            except (MediaError, TimeoutError) as error:
                logger.info("optional_photo_music code=%s", getattr(error, "code", "timeout"))
                music = None
        if directory_size(directory) > self.settings.max_download_bytes:
            raise MediaError(T.SERVICES_TIKTOK_PHOTO_POST_TOO_LARGE, code="file_too_large")
        media = []
        for index, image in enumerate(images):
            urls = post.motion[index] if index < len(post.motion) else ()
            asset = Asset(image.name, "photo")
            if urls:
                source = directory / f"live-{index:03}.mp4"
                await self._download_candidates(urls, source, self.settings.max_download_bytes)
                motion, _ = await self.media.compatible_video(source, directory)
                target = directory / f"motion-{index:03}.mp4"
                motion.path(directory).replace(target)
                asset = Asset(target.name, "video", motion.duration, motion.width, motion.height)
            media.append(asset)
        video, has_audio = None, False
        has_motion = any(asset.kind == "video" for asset in media)
        if music and not has_motion and not post.story and not re.search(r"/stor(?:y|ies)(?:/|$)", link.url):
            try:
                video, has_audio = await self.media.slideshow(images, music, directory)
            except (MediaError, TimeoutError) as error:
                logger.info("optional_slideshow code=%s", getattr(error, "code", "timeout"))
        return MediaBundle(
            directory,
            link.url,
            link.platform,
            post.title,
            video,
            tuple(Asset(image.name, "photo") for image in images),
            audio_asset,
            has_audio,
            tuple(media) if has_motion else (),
        )

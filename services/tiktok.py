"""TikTok Photo Mode from public page state; no third-party downloader APIs."""

import json
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


@dataclass(frozen=True)
class PhotoPost:
    title: str
    images: tuple[tuple[str, ...], ...]
    music: tuple[str, ...]


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
        return _urls(value.get("urlList") or value.get("url_list") or value.get("url") or [])
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


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
                    )
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
        match = re.search(r"/(?:photo|video)/(\d+)", response.url)
        post = parse_photo_page(response.body.decode("utf-8", errors="replace"), match[1] if match else None)
        if post is None:
            return None
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
            images.append(original)
            if directory_size(directory) > self.settings.max_download_bytes:
                raise MediaError(T.SERVICES_TIKTOK_PHOTO_POST_TOO_LARGE, code="file_too_large")
        music = None
        audio_asset = None
        if post.music:
            music = directory / "music.bin"
            await self._download_candidates(
                post.music, music, min(self.settings.max_download_bytes, self.settings.max_upload_bytes)
            )
            audio_asset = await self.media.extract_audio(music, directory)
            if audio_asset is None:
                raise MediaError(T.SERVICES_TIKTOK_MUSIC_UNAVAILABLE, code="invalid_audio")
            music = directory / audio_asset.filename
        if directory_size(directory) > self.settings.max_download_bytes:
            raise MediaError(T.SERVICES_TIKTOK_PHOTO_POST_TOO_LARGE, code="file_too_large")
        video, has_audio = await self.media.slideshow(images, music, directory)
        return MediaBundle(
            directory,
            link.url,
            link.platform,
            post.title,
            video,
            tuple(Asset(image.name, "document") for image in images),
            audio_asset,
            has_audio,
        )

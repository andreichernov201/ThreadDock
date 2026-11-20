"""Gallery extraction in an isolated CLI; every media download uses PublicHttp.

gallery-dl is a separately distributed GPL-2.0 dependency. No upstream code is
copied into this MIT project and no user configuration is loaded. TikTok's
optional public motion discovery is shared with the direct page adapter.
"""

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from config import Settings
from core import texts as T
from core.errors import MediaError
from core.models import Asset, MediaBundle
from services.http import PublicHttp
from services.links import VideoLink, normalize_url
from services.media import MediaTools
from services.network import SafeProxy
from services.processes import directory_size, run_process
from services.tiktok import PhotoPost, TikTokPhotos, photo_motion_urls
from services.ytdlp import source_error

logger = logging.getLogger(__name__)


def gallery_target(link: VideoLink) -> tuple[str, str]:
    """Translate individual TikTok Story URLs without returning unrelated stories."""
    match = re.fullmatch(r"/(@[\w.-]+)/stor(?:y|ies)/(\d+)/?", urlsplit(link.url).path)
    if link.platform == "TikTok" and match:
        return f"https://www.tiktok.com/{match[1]}/stories", match[2]
    return link.url, ""


def parse_gallery(payload: bytes, maximum: int, story_id: str = "") -> list[tuple[str, dict]]:
    try:
        messages = json.loads(payload)
        if not isinstance(messages, list):
            raise ValueError
        items = []
        seen = set()
        for message in messages:
            if not isinstance(message, list) or not message:
                raise ValueError
            if message[0] == -1:
                detail = message[-1]
                raise source_error(str(detail).encode())
            if message[0] != 3:
                continue
            _, url, metadata = message
            if not isinstance(url, str) or not isinstance(metadata, dict):
                raise ValueError
            if story_id and str(metadata.get("id")) != story_id:
                continue
            if url not in seen:
                seen.add(url)
                items.append((url, metadata))
            if len(items) > maximum:
                raise MediaError(T.SERVICES_TIKTOK_TOO_MANY_PHOTOS, code="photo_count")
        if not items:
            raise MediaError(T.SERVICES_YTDLP_UNAVAILABLE, code="unavailable")
        return items
    except (ValueError, TypeError, KeyError) as error:
        raise MediaError(T.SERVICES_YTDLP_DOWNLOAD_FAILED, code="gallery_metadata") from error


class GalleryAdapter:
    def __init__(self, settings: Settings, http: PublicHttp, tools: MediaTools):
        self.settings, self.http, self.tools = settings, http, tools

    async def download(self, link: VideoLink, directory: Path) -> MediaBundle:
        if link.platform == "TikTok" and urlsplit(link.url).hostname in {"vm.tiktok.com", "vt.tiktok.com"}:
            response = await self.http.get(link.url, 8 * 1024**2)
            link = normalize_url(response.url)
            if link.platform != "TikTok":
                raise MediaError(T.SERVICES_YTDLP_UNAVAILABLE, code="unavailable")
        target, story_id = gallery_target(link)
        async with SafeProxy() as proxy:
            command = [
                self.settings.gallerydl,
                "--config-ignore",
                "--no-input",
                "--no-colors",
                "--no-postprocessors",
                "--resolve-json",
                "--proxy",
                proxy.url,
                "--retries",
                "0",
                "--http-timeout",
                "15",
                "--range",
                f"1-{self.settings.max_photos + 1}",
                "-o",
                "cache.file=null",
                "-o",
                "extractor.netrc=false",
                "-o",
                "extractor.sleep-request=0",
                "-o",
                "extractor.tiktok.audio=true",
            ]
            if self.settings.cookies_file:
                cookies = directory / "gallery-cookies.txt"
                shutil.copyfile(self.settings.cookies_file, cookies)
                command += ["--cookies", str(cookies)]
            result = await run_process(
                command + ["--", target],
                timeout=min(self.settings.download_timeout, self.settings.extractor_timeout),
                cwd=directory,
                output_limit=16 * 1024**2,
            )
        if result.returncode:
            raise source_error(result.stderr)
        entries = parse_gallery(result.stdout, self.settings.max_photos, story_id)
        return await self.render_entries(link, directory, entries)

    async def render_entries(
        self, link: VideoLink, directory: Path, entries: list[tuple[str, dict]], *, extract_audio: bool = False
    ) -> MediaBundle:
        # The same motion discovery/rendering must run when the independent
        # extractor succeeds after the direct page failed.
        image_entries = [(url, metadata) for url, metadata in entries if metadata.get("type") == "image"]
        ids = {str(metadata.get("id") or "") for _, metadata in entries}
        if (
            link.platform == "TikTok"
            and image_entries
            and len(ids) == 1
            and all(metadata.get("type") in {"image", "audio"} for _, metadata in entries)
        ):
            metadata = image_entries[0][1]
            post = PhotoPost(
                str(metadata.get("title") or metadata.get("desc") or "TikTok")[:500],
                tuple((url,) for url, _ in image_entries),
                tuple(url for url, info in entries if info.get("type") == "audio"),
                tuple(photo_motion_urls(info.get("image") or {}) for _, info in image_entries),
                bool(metadata.get("story") or metadata.get("is_story")),
                next(iter(ids)),
                not any(metadata.get(key) for key in ("privateItem", "is_private", "forFriend", "secret")),
            )
            return await TikTokPhotos(self.settings, self.http, self.tools).render_post(link, directory, post)
        assets: list[Asset] = []
        audio = None
        title = link.platform
        for index, (url, metadata) in enumerate(entries):
            extension = str(metadata.get("extension", "")).lower()
            is_music = metadata.get("type") == "audio" or extension in {"mp3", "m4a", "aac", "ogg", "opus", "wav"}
            source = (
                directory / f"gallery-{index:03}.{extension if extension.isalnum() and len(extension) < 8 else 'bin'}"
            )
            try:
                if url.startswith("http://"):
                    url = "https://" + url[7:]
                await self.http.get(url, self.settings.max_download_bytes, destination=source)
                info = await self.tools.probe(source)
                if is_music:
                    audio = await self.tools.extract_audio(source, directory)
                elif info.duration > 0 and (
                    info.video_codec in {"h264", "hevc", "vp8", "vp9", "gif"}
                    or extension not in {"jpg", "jpeg", "png", "webp", "avif", "heic"}
                ):
                    # Each conversion has its own output name, preserving mixed-post order.
                    asset, _ = await self.tools.compatible_video(source, directory)
                    output = directory / f"media-{index:03}.mp4"
                    asset.path(directory).replace(output)
                    assets.append(Asset(output.name, "video", asset.duration, asset.width, asset.height))
                else:
                    assets.append(await self.tools.photo(source, directory))
                title = str(metadata.get("title") or metadata.get("description") or title)[:500]
            except (MediaError, TimeoutError) as error:
                if not is_music:
                    raise
                logger.info("optional_gallery_audio code=%s", getattr(error, "code", "timeout"))
            if directory_size(directory) > self.settings.max_download_bytes * 3:
                raise MediaError(T.SERVICES_PROCESSES_DOWNLOAD_TOO_LARGE, code="file_too_large")
        if not assets:
            raise MediaError(T.SERVICES_YTDLP_UNAVAILABLE, code="unavailable")
        if extract_audio and audio is None and len(assets) == 1 and assets[0].kind == "video":
            try:
                async with asyncio.timeout(min(10, self.settings.process_timeout)):
                    audio = await self.tools.extract_audio(assets[0].path(directory), directory)
            except (MediaError, TimeoutError) as error:
                logger.info("optional_gallery_audio code=%s", getattr(error, "code", "timeout"))
        photos = tuple(asset for asset in assets if asset.kind == "photo")
        if len(photos) == len(assets):
            video, has_audio = None, False
            # Story photos stay photos, including those with music stickers.
            story = any(metadata.get("story") or metadata.get("is_story") for _, metadata in entries)
            if audio and not story and not re.search(r"/stor(?:y|ies)(?:/|$)", link.url):
                try:
                    video, has_audio = await self.tools.slideshow(
                        [asset.path(directory) for asset in photos], audio.path(directory), directory
                    )
                except (MediaError, TimeoutError) as error:
                    logger.info("optional_gallery_slideshow code=%s", getattr(error, "code", "timeout"))
            return MediaBundle(directory, link.url, link.platform, title, video, photos, audio, has_audio)
        return MediaBundle(directory, link.url, link.platform, title, None, photos, audio, media=tuple(assets))

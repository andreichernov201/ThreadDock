"""Shared media service for every message source. No Telegram imports."""

import asyncio
import logging
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from pathlib import Path

from config import Settings
from core import texts as T
from core.errors import MediaError
from core.models import MediaBundle
from services.gallery import GalleryAdapter
from services.http import PublicHttp
from services.links import normalize_url
from services.media import MediaTools
from services.processes import directory_size, remove_directory
from services.tiktok import PhotoModeError, TikTokPhotos
from services.ytdlp import YtDlpAdapter
from storage.database import Database

logger = logging.getLogger(__name__)


class KeyedLocks:
    def __init__(self):
        self._entries: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: str):
        lock, users = self._entries.get(key, (asyncio.Lock(), 0))
        self._entries[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self._entries[key]
            if users == 1:
                del self._entries[key]
            else:
                self._entries[key] = (lock, users - 1)


class MediaPipeline:
    def __init__(self, settings: Settings, database: Database, http: PublicHttp):
        self.settings, self.database = settings, database
        self.tools = MediaTools(settings)
        self.video = YtDlpAdapter(settings)
        self.photos = TikTokPhotos(settings, http, self.tools)
        self.gallery = GalleryAdapter(settings, http, self.tools)
        self.locks = KeyedLocks()
        self.leases: dict[Path, int] = {}
        self.work = settings.data_dir / "work"
        self.cache = settings.data_dir / "cache"
        self.work.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)

    def clean_work(self) -> None:
        # Called once before workers start; no active jobs can own these directories.
        for directory in self.work.iterdir():
            if directory.is_dir() and not directory.is_symlink():
                remove_directory(directory, self.work)

    @contextmanager
    def using(self, bundle: MediaBundle):
        path = bundle.directory.resolve()
        self.leases[path] = self.leases.get(path, 0) + 1
        try:
            yield
        finally:
            self.leases[path] -= 1
            if not self.leases[path]:
                del self.leases[path]

    async def fetch(self, url: str, *, force: bool = False) -> MediaBundle:
        link = normalize_url(url)
        async with self.locks.hold(link.cache_key):
            cached = self.database.get_cache(link.cache_key)
            if cached and not cached.motion_pending and not force:
                logger.info("media_cache hit=disk platform=%s", link.platform)
                return cached
            logger.info("media_fetch platform=%s force=%s", link.platform, force)
            if (
                shutil.disk_usage(self.settings.data_dir).free
                < self.settings.min_free_bytes + self.settings.max_download_bytes * 2
            ):
                self.maintain(needed_free=self.settings.min_free_bytes + self.settings.max_download_bytes * 2)
                if (
                    shutil.disk_usage(self.settings.data_dir).free
                    < self.settings.min_free_bytes + self.settings.max_download_bytes * 2
                ):
                    raise MediaError(T.SERVICES_PIPELINE_INSUFFICIENT_DISK, code="disk_full", retryable=True)
            directory = Path(tempfile.mkdtemp(prefix="job-", dir=self.work))
            try:
                async with asyncio.timeout(self.settings.download_timeout + self.settings.process_timeout * 2):
                    bundle = None
                    photo_error = None
                    if link.platform == "TikTok" and "/video/" not in link.url:
                        try:
                            bundle = await self.photos.download(link, directory)
                        except MediaError as error:
                            photo_error = error
                            logger.info("tiktok_photo_probe_failed code=%s", error.code)
                    if bundle is None and link.platform in {"Instagram", "Reddit", "Pinterest", "TikTok", "X"}:
                        # For ordinary TikTok videos, prefer the quality-aware video
                        # extractor; gallery extraction is its independent fallback.
                        prefer_gallery = (
                            link.platform != "TikTok" or isinstance(photo_error, PhotoModeError) or "/stor" in link.url
                        )
                        if prefer_gallery:
                            try:
                                bundle = await self.gallery.download(link, directory)
                            except MediaError as error:
                                logger.info("gallery_probe_failed platform=%s code=%s", link.platform, error.code)
                                if isinstance(photo_error, PhotoModeError):
                                    raise photo_error from error
                                photo_error = photo_error or error
                    if bundle is None:
                        try:
                            source, title = await self.video.download(link, directory)
                        except MediaError as video_error:
                            if link.platform == "TikTok" and "/video/" in link.url:
                                # TikTok sometimes serves photo posts at a /video/ route.
                                # Only check after video extraction failed.
                                try:
                                    bundle = await self.photos.download(link, directory)
                                except MediaError as error:
                                    photo_error = error
                                    logger.info("tiktok_photo_fallback_failed code=%s", error.code)
                            if bundle is None:
                                if link.platform in {"TikTok", "X"}:
                                    try:
                                        bundle = await self.gallery.download(link, directory)
                                    except MediaError as error:
                                        logger.info("gallery_fallback_failed code=%s", error.code)
                            if bundle is None:
                                if photo_error and video_error.code == "extractor_failed":
                                    raise photo_error from video_error
                                raise
                        if bundle is None:
                            if isinstance(source, list):
                                from core.models import Asset

                                media = []
                                for index, item in enumerate(source):
                                    asset, _ = await self.tools.compatible_video(item, directory)
                                    target = directory / f"media-{index:03}.mp4"
                                    asset.path(directory).replace(target)
                                    media.append(Asset(target.name, "video", asset.duration, asset.width, asset.height))
                                bundle = MediaBundle(
                                    directory, link.url, link.platform, title, None, media=tuple(media)
                                )
                        if bundle is None:
                            video, has_audio = await self.tools.compatible_video(source, directory)
                            try:
                                async with asyncio.timeout(min(30, self.settings.process_timeout)):
                                    audio = await self.tools.extract_audio(source, directory)
                            except (MediaError, TimeoutError) as error:
                                logger.warning("optional_audio code=%s", getattr(error, "code", "timeout"))
                                audio = None
                            bundle = MediaBundle(
                                directory, link.url, link.platform, title, video, audio=audio, has_audio=has_audio
                            )
                    keep = {asset.filename for asset in bundle.assets}
                    for path in directory.iterdir():
                        if path.is_file() and path.name not in keep:
                            path.unlink()
                    # Never replace files another chat may currently be sending.
                    target = self.cache / (link.cache_key + "-" + uuid.uuid4().hex[:16])
                    directory.replace(target)
                    bundle = replace(bundle, directory=target)
                    self.database.put_cache(
                        link.cache_key,
                        bundle,
                        min(60, self.settings.cache_ttl) if bundle.motion_pending else self.settings.cache_ttl,
                    )
                    return bundle
            except TimeoutError as error:
                raise MediaError(T.SERVICES_PIPELINE_TIMEOUT, code="timeout", retryable=True) from error
            except OSError as error:
                raise MediaError(T.SERVICES_PIPELINE_SAVE_FAILED, code="storage_error", retryable=True) from error
            finally:
                if directory.exists():
                    remove_directory(directory, self.work)

    def maintain(self, *, needed_free: int = 0) -> None:
        total = directory_size(self.cache)
        free = shutil.disk_usage(self.settings.data_dir).free
        for row in self.database.cache_candidates():
            try:
                candidate = MediaBundle.deserialize(row["bundle"], self.settings.data_dir)
                if candidate.directory in self.leases:
                    continue
            except (ValueError, KeyError, TypeError):
                pass
            if row["expires_at"] > time.time() and total <= self.settings.cache_max_bytes and free >= needed_free:
                continue
            try:
                bundle = MediaBundle.deserialize(row["bundle"], self.settings.data_dir)
                if bundle.directory.parent != self.cache.resolve():
                    raise ValueError("Invalid cache parent")
                size = directory_size(bundle.directory)
                remove_directory(bundle.directory, self.cache)
                total -= size
                free += size
            except (OSError, ValueError):
                pass
            self.database.delete_cache(row["key"])
        # A crash between an atomic directory rename and SQLite commit can leave an orphan.
        known = self.database.cache_keys() | self.leases.keys()
        for directory in self.cache.iterdir():
            if (
                directory.is_dir()
                and not directory.is_symlink()
                and len(directory.name.split("-")[0]) == 64
                and all(char in "0123456789abcdef-" for char in directory.name)
                and directory.resolve() not in known
            ):
                remove_directory(directory, self.cache)
        self.database.prune_jobs(self.settings.retention_days)

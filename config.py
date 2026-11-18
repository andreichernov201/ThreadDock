"""Environment configuration. Importing this module has no side effects."""

import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from core import texts as T

PROJECT_ROOT = Path(__file__).resolve().parent


def _integer(name: str, default: int, minimum: int = 1, maximum: int = 10**9) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(T.CONFIG_INTEGER_REQUIRED.format(name=name)) from error
    if not minimum <= value <= maximum:
        raise ValueError(T.CONFIG_INTEGER_RANGE.format(name=name, minimum=minimum, maximum=maximum))
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(T.CONFIG_BOOLEAN_REQUIRED.format(name=name))
    return value in {"true", "1", "yes"}


def _ids(name: str) -> frozenset[int]:
    try:
        return frozenset(int(value.strip()) for value in os.getenv(name, "").split(",") if value.strip())
    except ValueError as error:
        raise ValueError(T.CONFIG_ID_LIST_REQUIRED.format(name=name)) from error


@dataclass(frozen=True)
class Settings:
    token: str
    data_dir: Path = PROJECT_ROOT / "data"
    workers: int = 2
    queue_capacity: int = 200
    download_timeout: int = 300
    extractor_timeout: int = 45
    process_timeout: int = 300
    max_download_bytes: int = 500 * 1024 * 1024
    max_upload_bytes: int = 49_000_000
    max_duration: int = 1800
    max_photos: int = 100
    cache_ttl: int = 3 * 86400
    cache_max_bytes: int = 5 * 1024**3
    min_free_bytes: int = 1024**3
    retries: int = 2
    retention_days: int = 7
    shutdown_timeout: int = 30
    ytdlp: str = "yt-dlp"
    gallerydl: str = "gallery-dl"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    deno: str = "deno"
    cookies_file: Path | None = None
    tiktok_live_photos: bool = True
    groups_enabled: frozenset[int] = frozenset()
    business_enabled: bool = True
    business_chats: frozenset[int] = frozenset()
    api_base: str = ""

    def __post_init__(self) -> None:
        positive = (
            self.workers,
            self.queue_capacity,
            self.download_timeout,
            self.extractor_timeout,
            self.process_timeout,
            self.max_download_bytes,
            self.max_upload_bytes,
            self.cache_ttl,
            self.cache_max_bytes,
            self.min_free_bytes,
            self.max_duration,
            self.max_photos,
        )
        if any(not math.isfinite(value) or value <= 0 for value in positive):
            raise ValueError(T.CONFIG_POSITIVE_SETTINGS_REQUIRED)
        if not self.api_base and self.max_upload_bytes > 50_000_000:
            raise ValueError(T.CONFIG_LOCAL_API_REQUIRED)


def load_settings(*, require_token: bool = True) -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("BOT_TOKEN", "").strip()
    if require_token and not token:
        raise ValueError(T.CONFIG_TOKEN_REQUIRED)
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    cookies = os.getenv("COOKIES_FILE", "").strip()
    return Settings(
        token=token,
        data_dir=data_dir.resolve(),
        workers=_integer("DOWNLOAD_WORKERS", 2, maximum=16),
        queue_capacity=_integer("QUEUE_CAPACITY", 200),
        download_timeout=_integer("DOWNLOAD_TIMEOUT_SECONDS", 300, maximum=7200),
        extractor_timeout=_integer("EXTRACTOR_TIMEOUT_SECONDS", 45, maximum=300),
        process_timeout=_integer("PROCESS_TIMEOUT_SECONDS", 300, maximum=7200),
        max_download_bytes=_integer("MAX_DOWNLOAD_MB", 500) * 1024**2,
        max_upload_bytes=_integer("MAX_UPLOAD_MB", 49, maximum=2000) * 1_000_000,
        max_duration=_integer("MAX_DURATION_SECONDS", 1800),
        max_photos=_integer("MAX_PHOTOS", 100, maximum=1000),
        cache_ttl=min(72, _integer("CACHE_TTL_HOURS", 72)) * 3600,
        cache_max_bytes=_integer("CACHE_MAX_MB", 5120) * 1024**2,
        min_free_bytes=_integer("MIN_FREE_DISK_MB", 1024) * 1024**2,
        retries=_integer("DOWNLOAD_RETRIES", 2, minimum=0, maximum=10),
        retention_days=_integer("JOB_RETENTION_DAYS", 7),
        shutdown_timeout=_integer("SHUTDOWN_TIMEOUT_SECONDS", 30),
        ytdlp=os.getenv("YTDLP_EXECUTABLE", "yt-dlp"),
        gallerydl=os.getenv("GALLERYDL_EXECUTABLE", "gallery-dl"),
        ffmpeg=os.getenv("FFMPEG_EXECUTABLE", "ffmpeg"),
        ffprobe=os.getenv("FFPROBE_EXECUTABLE", "ffprobe"),
        deno=os.getenv("DENO_EXECUTABLE", "deno"),
        cookies_file=(PROJECT_ROOT / cookies).resolve() if cookies else None,
        tiktok_live_photos=_boolean("TIKTOK_LIVE_PHOTOS", True),
        groups_enabled=_ids("ALLOWED_GROUP_IDS"),
        business_enabled=_boolean("ENABLE_BUSINESS", True),
        business_chats=_ids("BUSINESS_ALLOWED_CHAT_IDS"),
        api_base=os.getenv("BOT_API_BASE_URL", "").strip().rstrip("/"),
    )


def get_bot_token() -> str:
    return load_settings().token

"""yt-dlp adapter. Downloads through the guarded proxy; FFmpeg reads local files."""

import json
import re
import shutil
from pathlib import Path

from config import Settings
from core import texts as T
from core.errors import MediaError
from services.links import VideoLink
from services.network import SafeProxy
from services.processes import run_process

SAFE_PROTOCOL = "[protocol~='^(https|m3u8_native|http_dash_segments)$']"
FORMAT = f"bv{SAFE_PROTOCOL}+ba{SAFE_PROTOCOL}/b{SAFE_PROTOCOL}/bv{SAFE_PROTOCOL}"


def build_command(settings: Settings, link: VideoLink, directory: Path, proxy: str) -> list[str]:
    command = [
        settings.ytdlp,
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-remote-components",
        "--no-cache-dir",
        "--no-playlist",
        "--playlist-items",
        f"1-{settings.max_photos + 1}",
        "--no-progress",
        "--no-warnings",
        "--quiet",
        "--no-geo-bypass",
        "--proxy",
        proxy,
        "--geo-verification-proxy",
        proxy,
        "--socket-timeout",
        "15",
        "--retries",
        "3",
        "--fragment-retries",
        "3",
        "--extractor-retries",
        "2",
        "--retry-sleep",
        "http:exp=1:5",
        "--abort-on-unavailable-fragments",
        "--max-filesize",
        str(settings.max_download_bytes),
        "--match-filter",
        "!is_live & !is_upcoming",
        "--format",
        FORMAT,
        "--downloader",
        "native",
        "--merge-output-format",
        "mkv",
        "--fixup",
        "never",
        "--ffmpeg-location",
        shutil.which(settings.ffmpeg) or settings.ffmpeg,
        "--postprocessor-args",
        "ffmpeg_i:-protocol_whitelist file,pipe,crypto",
        # Native HLS can fall back to FFmpeg. That fallback must never open remote URLs.
        "--downloader-args",
        "ffmpeg_i:-protocol_whitelist file,pipe,crypto",
        "--no-js-runtimes",
        "--js-runtimes",
        f"deno:{shutil.which(settings.deno) or settings.deno}",
        "--write-info-json",
        "--output",
        str(directory / "source-%(autonumber)03d.%(ext)s"),
    ]
    if settings.cookies_file:
        # yt-dlp writes back its cookie jar: never modify the operator's original.
        cookies = directory / "cookies.txt"
        shutil.copyfile(settings.cookies_file, cookies)
        command += ["--cookies", str(cookies)]
    return [*command, "--", link.url]


def source_error(stderr: bytes) -> MediaError:
    text = stderr.decode("utf-8", errors="replace").lower()
    # Signed URL parameters may themselves contain words such as "private" or "404".
    text = re.sub(r"https?://\S+", "[url]", text)
    if any(
        word in text
        for word in ("timed out", "429", "502", "503", "temporary", "resolve", "connection", "reset by peer")
    ):
        return MediaError(T.SERVICES_YTDLP_SOURCE_UNAVAILABLE, code="source_temporary", retryable=True)
    if any(word in text for word in ("403", "captcha", "challenge", "status code 0", "impersonat", "cloudflare")):
        return MediaError(T.SERVICES_YTDLP_BLOCKED, code="source_blocked", retryable=True)
    if any(word in text for word in ("private", "login required", "log in", "sign in", "cookies", "age-restricted")):
        return MediaError(T.SERVICES_YTDLP_RESTRICTED, code="authentication")
    if any(word in text for word in ("max-filesize", "larger than max", "file is larger")):
        return MediaError(T.SERVICES_YTDLP_DOWNLOAD_TOO_LARGE, code="file_too_large")
    if "requested format" in text or "no video formats" in text:
        return MediaError(T.SERVICES_YTDLP_FORMAT_UNAVAILABLE, code="format_unavailable")
    if any(word in text for word in ("not available", "has been removed", "does not exist", "404", "unavailable")):
        return MediaError(T.SERVICES_YTDLP_UNAVAILABLE, code="unavailable")
    return MediaError(T.SERVICES_YTDLP_DOWNLOAD_FAILED, code="extractor_failed")


class YtDlpAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def download(self, link: VideoLink, directory: Path) -> tuple[Path | list[Path], str]:
        async with SafeProxy() as proxy:
            result = await run_process(
                build_command(self.settings, link, directory, proxy.url),
                timeout=self.settings.download_timeout,
                watch_directory=directory,
                max_disk_bytes=self.settings.max_download_bytes * 3,
            )
        if result.returncode:
            raise source_error(result.stderr)
        files = [
            path
            for path in sorted(directory.glob("source-*"))
            if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi", ".ts"} and not path.is_symlink()
        ]
        if not files or any(path.stat().st_size == 0 for path in files):
            raise MediaError(T.SERVICES_YTDLP_NO_VIDEO, code="no_video")
        if (
            len(files) > self.settings.max_photos
            or sum(path.stat().st_size for path in files) > self.settings.max_download_bytes
        ):
            raise MediaError(T.SERVICES_YTDLP_DOWNLOAD_TOO_LARGE, code="file_too_large")
        title = link.platform
        metadata = next(directory.glob("source-*.info.json"), directory / "missing.json")
        if metadata.is_file() and metadata.stat().st_size < 8 * 1024**2:
            try:
                title = str(json.loads(metadata.read_text(encoding="utf-8")).get("title") or title)[:500]
            except (ValueError, OSError):
                pass
        return files[0] if len(files) == 1 else files, title

"""Startup checks deliberately run before opening any Telegram session."""

import shutil
import subprocess

from config import Settings
from core import texts as T


def validate_runtime(settings: Settings, *, require_token: bool = True) -> None:
    import sqlite3

    from archive.config import ArchiveSettings

    ArchiveSettings.from_environment()
    with sqlite3.connect(":memory:") as probe:
        probe.execute("CREATE VIRTUAL TABLE archive_probe USING fts5(body)")
    if require_token:
        from aiogram.utils.token import TokenValidationError, validate_token

        try:
            validate_token(settings.token)
        except TokenValidationError as error:
            raise ValueError(T.SERVICES_RUNTIME_INVALID_TOKEN) from error
    for name, executable, flag in (
        ("yt-dlp", settings.ytdlp, "--version"),
        ("gallery-dl", settings.gallerydl, "--version"),
        ("FFmpeg", settings.ffmpeg, "-version"),
        ("ffprobe", settings.ffprobe, "-version"),
        ("Deno", settings.deno, "--version"),
    ):
        resolved = shutil.which(executable)
        if not resolved:
            raise ValueError(T.SERVICES_RUNTIME_EXECUTABLE_MISSING.format(name=name))
        try:
            result = subprocess.run([resolved, flag], capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(T.SERVICES_RUNTIME_EXECUTABLE_START_FAILED.format(name=name)) from error
        if result.returncode:
            raise ValueError(T.SERVICES_RUNTIME_EXECUTABLE_CHECK_FAILED.format(name=name))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if settings.cookies_file and not settings.cookies_file.is_file():
        raise ValueError(T.SERVICES_RUNTIME_COOKIE_FILE_MISSING)


def check_configuration(require_token: bool = True) -> int:
    from config import load_settings

    try:
        validate_runtime(load_settings(require_token=require_token), require_token=require_token)
    except Exception as error:
        # Token validation exceptions include no token, but keep output explicit and fixed.
        print(str(error) if isinstance(error, ValueError) else T.SERVICES_RUNTIME_CHECK_FAILED)
        return 1
    print(T.SERVICES_RUNTIME_CHECK_PASSED)
    return 0

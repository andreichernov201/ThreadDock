"""Optional discovery of public Live Photo clips missing from TikTok's web state.

Only a public post ID is sent to TikWM. Its result must match the post and every
still image before any motion is accepted. Media bytes come from TikTok's CDN,
through PublicHttp; resolver/CDN URLs and response bodies are never persisted.
"""

import asyncio
import json
import logging
from urllib.parse import unquote, urlsplit

from core.errors import MediaError
from services.http import PublicHttp, check_https_url

logger = logging.getLogger(__name__)
CDN_SUFFIXES = ("tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com", "tiktokv.com")


def image_identity(url: str) -> str:
    return unquote(urlsplit(url).path).rsplit("/", 1)[-1].split("~", 1)[0]


def parse_live_photos(
    payload: bytes, identifier: str, images: tuple[tuple[str, ...], ...]
) -> tuple[tuple[str, ...], ...] | None:
    """Fail closed on wrong posts, ambiguous slide positions or non-TikTok media."""
    empty = tuple(() for _ in images)
    try:
        response = json.loads(payload)
        data = response.get("data")
        if response.get("code") != 0 or not isinstance(data, dict) or str(data.get("id")) != identifier:
            return None
        stills, clips = data.get("images"), data.get("live_images")
        if not isinstance(stills, list) or len(stills) != len(images):
            return None
        for expected, actual in zip(images, stills, strict=True):
            if not isinstance(actual, str) or not any(
                image_identity(url) == image_identity(actual) for url in expected
            ):
                return None
        if clips is None or clips == []:
            return empty
        if not isinstance(clips, list) or len(clips) != len(images):
            return None
        result = []
        for clip in clips:
            if not clip:
                result.append(())
                continue
            if not isinstance(clip, str):
                return None
            check_https_url(clip)
            host = urlsplit(clip).hostname or ""
            if not any(host == suffix or host.endswith("." + suffix) for suffix in CDN_SUFFIXES):
                return None
            result.append((clip,))
        return tuple(result)
    except (ValueError, TypeError, AttributeError, MediaError):
        return None


async def public_live_photos(
    http: PublicHttp, identifier: str, images: tuple[tuple[str, ...], ...]
) -> tuple[tuple[str, ...], ...] | None:
    if not identifier.isascii() or not identifier.isdigit() or not 10 <= len(identifier) <= 25:
        return None
    try:
        async with asyncio.timeout(8):
            response = await http.get_browser(f"https://www.tikwm.com/api/?url={identifier}&hd=1", 2 * 1024**2)
        motion = parse_live_photos(response.body, identifier, images)
        logger.info(
            "tiktok_live_lookup clips=%s", sum(bool(item) for item in motion) if motion is not None else "unknown"
        )
        return motion
    except (MediaError, TimeoutError) as error:
        logger.info("optional_tiktok_live_lookup code=%s", getattr(error, "code", "timeout"))
        return None


async def public_video_urls(http: PublicHttp, identifier: str) -> tuple[str, ...]:
    """Resolve only an ID already matched to a public TikTok video page."""
    if not identifier.isascii() or not identifier.isdigit() or not 10 <= len(identifier) <= 25:
        return ()
    try:
        async with asyncio.timeout(8):
            response = await http.get_browser(f"https://www.tikwm.com/api/?url={identifier}&hd=1", 2 * 1024**2)
        payload = json.loads(response.body)
        data = payload.get("data")
        if payload.get("code") != 0 or not isinstance(data, dict) or str(data.get("id")) != identifier:
            return ()
        urls = []
        for key in ("hdplay", "play"):
            url = data.get(key)
            if not isinstance(url, str):
                continue
            check_https_url(url)
            host = urlsplit(url).hostname or ""
            if host == "www.tikwm.com" or any(host == s or host.endswith("." + s) for s in CDN_SUFFIXES):
                urls.append(url)
        return tuple(dict.fromkeys(urls))
    except (MediaError, TimeoutError, ValueError, AttributeError, TypeError):
        return ()

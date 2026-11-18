"""First-party TikTok player metadata. No hosted downloader or resolver APIs."""

import asyncio
import json
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from core.errors import MediaError
from services.http import PublicHttp, check_https_url

CDN_SUFFIXES = ("tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com", "tiktokv.com", "tiktokv.us", "tiktok.com")


def image_identity(url: str) -> str:
    return unquote(urlsplit(url).path).rsplit("/", 1)[-1].split("~", 1)[0]


def checked_url(url: str) -> str:
    if not isinstance(url, str):
        raise ValueError("Invalid media URL")
    check_https_url(url)
    host = urlsplit(url).hostname or ""
    if not any(host == suffix or host.endswith("." + suffix) for suffix in CDN_SUFFIXES):
        raise ValueError("Not a TikTok media origin")
    return url


def video_candidates(urls) -> tuple[str, ...]:
    result = []
    for url in urls:
        try:
            checked_url(url)
            parts = urlsplit(url)
            query = parse_qs(parts.query)
            if (
                parts.hostname == "www.tiktok.com"
                and parts.path == "/aweme/v1/play/"
                and all(query.get(key) for key in ("video_id", "file_id"))
                and any(query.get(key) for key in ("sign", "signaturev3", "signv3"))
            ):
                # Same signed resource, served by TikTok's mobile playback host.
                result.append(urlunsplit(("https", "api16-normal-useast8.tiktokv.us", parts.path, parts.query, "")))
            result.append(url)
        except (ValueError, TypeError, MediaError):
            continue
    return tuple(
        sorted(
            dict.fromkeys(result),
            key=lambda url: (
                "webapp-prime" in urlsplit(url).hostname,
                urlsplit(url).hostname == "www.tiktok.com",
            ),
        )
    )


def parse_player_item(payload: bytes, identifier: str) -> dict | None:
    try:
        data = json.loads(payload)
        if data.get("status_code") != 0:
            return None
        return next(
            (
                item
                for item in data.get("items", [])
                if isinstance(item, dict) and str(item.get("id_str") or item.get("id")) == identifier
            ),
            None,
        )
    except (ValueError, TypeError, AttributeError):
        return None


async def player_item(http: PublicHttp, identifier: str) -> dict | None:
    if not identifier.isascii() or not identifier.isdigit() or not 10 <= len(identifier) <= 25:
        return None
    try:
        async with asyncio.timeout(8):
            response = await http.get_browser(
                f"https://www.tiktok.com/player/api/v1/items?item_ids={identifier}&aid=1988", 8 * 1024**2
            )
        return parse_player_item(response.body, identifier)
    except (MediaError, TimeoutError):
        return None


def parse_live_photos(payload: bytes, identifier: str, images: tuple[tuple[str, ...], ...]):
    item = parse_player_item(payload, identifier)
    if item is None:
        return None
    try:
        stills = item["image_post_info"]["images"]
        if len(stills) != len(images):
            return None
        result = []
        for expected, actual in zip(images, stills, strict=True):
            urls = actual["display_image"]["url_list"]
            if not any(image_identity(a) == image_identity(b) for a in expected for b in urls):
                return None
            video = actual.get("video") or actual.get("live_photo_video") or {}
            clips = video.get("url_list") or video.get("play_addr", {}).get("url_list") or []
            result.append(tuple(checked_url(url) for url in clips))
        # Absence of motion in this endpoint does not prove the source is static.
        return tuple(result) if any(result) else None
    except (KeyError, TypeError, ValueError, AttributeError, MediaError):
        return None


async def public_live_photos(http: PublicHttp, identifier: str, images: tuple[tuple[str, ...], ...]):
    item = await player_item(http, identifier)
    if item is None:
        return None
    return parse_live_photos(json.dumps({"status_code": 0, "items": [item]}).encode(), identifier, images)


async def public_video_urls(http: PublicHttp, identifier: str) -> tuple[str, ...]:
    item = await player_item(http, identifier)
    if item is None or item.get("image_post_info"):
        return ()
    try:
        profiles = item.get("video_info", {}).get("profiles", [])
        profiles = [p for p in profiles if p.get("codec_type") in {"h264", "h265"}]
        profiles.sort(key=lambda p: (p.get("codec_type") == "h264", int(p.get("bitrate") or 0)), reverse=True)
        return tuple(
            dict.fromkeys(
                url
                for profile in profiles
                for url in video_candidates(profile.get("play_addr", {}).get("url_list", []))
            )
        )
    except (ValueError, TypeError, AttributeError):
        return ()

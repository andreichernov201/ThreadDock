"""Bounded first-party Instagram embed metadata; media still uses PublicHttp."""

import asyncio
import json
import re
from http.cookiejar import CookieJar, MozillaCookieJar

from config import Settings
from core import texts as T
from core.errors import MediaError
from services.http import PublicHttp, check_https_url
from services.links import VideoLink


class InstagramGalleryError(MediaError):
    """A known photo/carousel post must not degrade into a video-only result."""


def video_post_id(url: str) -> str | None:
    from urllib.parse import urlsplit

    match = re.fullmatch(r"/(?:[\w.]+/)?(?:reels?|tv)/([A-Za-z0-9_-]{1,64})/?", urlsplit(url).path)
    return match[1] if match else None


def parse_embed(page: str, expected_id: str, maximum: int) -> list[tuple[str, dict]]:
    """Read the declared post, never a suggested video or its cover image."""
    gallery = False
    try:
        match = re.search(r'"contextJSON"\s*:\s*', page)
        if match is None:
            raise ValueError
        value, _ = json.JSONDecoder().raw_decode(page[match.end() :])
        data = json.loads(value) if isinstance(value, str) else value
        media = data["gql_data"]["shortcode_media"]
        if not isinstance(media, dict) or media.get("shortcode") != expected_id:
            raise ValueError
        if media.get("is_private"):
            raise ValueError
        sidecar = media.get("edge_sidecar_to_children")
        gallery = sidecar is not None or media.get("is_video") is False
        if sidecar is not None:
            nodes = [entry["node"] for entry in sidecar["edges"]]
        else:
            nodes = [media]
        if len(nodes) > maximum:
            raise MediaError(T.SERVICES_TIKTOK_TOO_MANY_PHOTOS, code="photo_count")
        if not nodes:
            raise ValueError
        entries = []
        for node in nodes:
            # Missing video_url must not silently turn a video into its still cover.
            kind = "video" if node.get("is_video") is True else "image" if node.get("is_video") is False else None
            url = node.get("video_url") if kind == "video" else node.get("display_url") if kind == "image" else None
            if not isinstance(url, str) or not url:
                raise ValueError
            check_https_url(url)
            entries.append((url, {"type": kind, "extension": "mp4" if kind == "video" else "jpg"}))
        return entries
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        kind = InstagramGalleryError if gallery else MediaError
        raise kind(T.DOWNLOAD_UNAVAILABLE, code="instagram_embed_missing") from error


class InstagramEmbed:
    def __init__(self, settings: Settings, http: PublicHttp):
        self.settings, self.http = settings, http

    async def entries(self, link: VideoLink) -> list[tuple[str, dict]]:
        identifier = video_post_id(link.url)
        if link.platform != "Instagram" or identifier is None:
            raise MediaError(T.DOWNLOAD_UNAVAILABLE, code="instagram_embed_missing")
        page = await self._page(identifier)
        return parse_embed(page, identifier, self.settings.max_photos)

    async def _page(self, identifier: str) -> str:
        try:
            from curl_cffi.requests import AsyncSession, RequestsError
        except ImportError as error:
            raise MediaError(T.DOWNLOAD_UNAVAILABLE, code="instagram_embed_missing") from error
        jar = CookieJar()
        if self.settings.cookies_file:
            source = MozillaCookieJar(str(self.settings.cookies_file))
            try:
                source.load(ignore_discard=True)
            except (OSError, ValueError) as error:
                raise MediaError(T.DOWNLOAD_UNAVAILABLE, code="authentication") from error
            for cookie in source:
                if cookie.domain.lstrip(".").lower() == "instagram.com" or cookie.domain.lower().endswith(
                    ".instagram.com"
                ):
                    jar.set_cookie(cookie)
        try:
            async with asyncio.timeout(6):
                async with AsyncSession(
                    proxy=self.http.proxy.url, cookies=jar, impersonate="chrome", trust_env=False
                ) as session:
                    # Never follow an authenticated redirect or pass this client to
                    # media downloads. The only request is to this fixed origin.
                    response = await session.get(
                        f"https://www.instagram.com/p/{identifier}/embed/captioned/",
                        headers={"Referer": "https://www.instagram.com/"},
                        timeout=6,
                        allow_redirects=False,
                        stream=True,
                    )
                    try:
                        if response.status_code != 200:
                            raise MediaError(T.DOWNLOAD_UNAVAILABLE, code="instagram_embed_missing")
                        body = bytearray()
                        async for chunk in response.aiter_content():
                            body.extend(chunk)
                            if len(body) > 4 * 1024**2:
                                raise MediaError(T.DOWNLOAD_UNAVAILABLE, code="instagram_embed_missing")
                        return body.decode("utf-8", errors="replace")
                    finally:
                        await response.aclose()
        except (RequestsError, TimeoutError) as error:
            raise MediaError(T.DOWNLOAD_TRY_LATER, code="instagram_embed_missing") from error

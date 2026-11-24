"""YouTube Community images from the requested post's public page only."""

import asyncio
import json
import re
from urllib.parse import urlsplit, urlunsplit

from core import texts as T
from core.errors import MediaError
from services.http import PublicHttp, check_https_url
from services.links import VideoLink


def original_image(url: str) -> str:
    if not isinstance(url, str):
        raise ValueError("Missing image URL")
    if url.startswith("//"):
        url = "https:" + url
    check_https_url(url)
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not any(
        host == domain or host.endswith("." + domain) for domain in ("ggpht.com", "googleusercontent.com", "ytimg.com")
    ):
        raise ValueError("Not a YouTube image origin")
    # Google's advertised square thumbnails crop the original landscape image.
    # Its original rendition preserves the full image and file format.
    if not parts.query and host.endswith((".ggpht.com", ".googleusercontent.com")):
        path = re.sub(r"=(?:s|w|h)[0-9]+(?:-.*)?$", "=s0-d", parts.path)
        return urlunsplit(parts._replace(path=path))
    return url


def parse_post(page: str, expected_id: str, maximum: int) -> list[tuple[str, dict]]:
    try:
        match = re.search(r'(?:\bytInitialData|window\s*\[\s*["\']ytInitialData["\']\s*\])\s*=\s*', page)
        if not match:
            raise ValueError("Missing page data")
        data, _ = json.JSONDecoder().raw_decode(page[match.end() :])
        stack = [data]
        post = None
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                candidate = value.get("backstagePostRenderer")
                if isinstance(candidate, dict) and candidate.get("postId") == expected_id:
                    post = candidate
                    break
                stack.extend(v for v in value.values() if isinstance(v, (dict, list)))
            elif isinstance(value, list):
                stack.extend(value)
        if post is None:
            raise ValueError("Requested post is missing")
        attachment = post.get("backstageAttachment") or {}
        if "postMultiImageRenderer" in attachment:
            images = attachment["postMultiImageRenderer"]["images"]
            if not isinstance(images, list) or not images:
                raise ValueError("Incomplete carousel")
            nodes = [item["backstageImageRenderer"] for item in images]
        elif "backstageImageRenderer" in attachment:
            nodes = [attachment["backstageImageRenderer"]]
        else:
            raise MediaError(T.DOWNLOAD_NO_PHOTOS, code="no_photos")
        if len(nodes) > maximum:
            raise MediaError(T.SERVICES_TIKTOK_TOO_MANY_PHOTOS, code="photo_count")
        entries = []
        for node in nodes:
            thumbnails = node["image"]["thumbnails"]
            if not isinstance(thumbnails, list) or not thumbnails:
                raise ValueError("Missing carousel image")
            # A malformed member aborts the whole post rather than silently
            # returning an incomplete carousel or a suggested video's cover.
            choices = [t for t in thumbnails if isinstance(t, dict) and isinstance(t.get("url"), str)]
            best = max(choices, key=lambda t: int(t.get("width") or 0) * int(t.get("height") or 0))
            entries.append((original_image(best["url"]), {"type": "image", "extension": "jpg"}))
        return entries
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        raise MediaError(T.DOWNLOAD_UNAVAILABLE, code="unavailable") from error


class YouTubePosts:
    def __init__(self, http: PublicHttp, maximum: int):
        self.http, self.maximum = http, maximum

    async def entries(self, link: VideoLink) -> list[tuple[str, dict]]:
        identifier = link.youtube_post_id
        if not identifier:
            raise MediaError(T.DOWNLOAD_UNAVAILABLE, code="unavailable")
        try:
            async with asyncio.timeout(8):
                # ucbcb requests the public page without a consent interstitial;
                # no operator cookies or account session are exported to YouTube.
                result = await self.http.get(f"https://www.youtube.com/post/{identifier}?hl=en&ucbcb=1", 8 * 1024**2)
            return parse_post(result.body.decode("utf-8", errors="replace"), identifier, self.maximum)
        except TimeoutError as error:
            raise MediaError(T.DOWNLOAD_TRY_LATER, code="source_temporary", retryable=True) from error

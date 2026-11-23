"""Selected TikTok comments, fetched only from TikTok through PublicHttp."""

import asyncio
import json
import logging
import re
from urllib.parse import urlencode, urlsplit

from core import texts as T
from core.errors import MediaError
from services.http import PublicHttp
from services.links import LinkError, VideoLink, normalize_url

logger = logging.getLogger(__name__)


def selected_comment(payload: bytes, identifier: str, video_id: str) -> str:
    try:
        data = json.loads(payload)
        if data.get("status_code") != 0:
            return ""
        for item in data.get("comments") or ():
            if not isinstance(item, dict) or str(item.get("cid")) != identifier:
                continue
            if item.get("aweme_id") is not None and str(item["aweme_id"]) != video_id:
                continue
            text = item.get("text")
            if isinstance(text, str):
                return "".join(
                    c for c in text if c in "\n\t" or (ord(c) >= 32 and not 0xD800 <= ord(c) <= 0xDFFF)
                ).strip()
    except (ValueError, TypeError, AttributeError):
        pass
    return ""


def comment_caption(text: str) -> str:
    # Telegram measures entity offsets in UTF-16. Keep even emoji-only comments
    # within the caption limit without cutting a surrogate pair or parsing HTML.
    value = T.DOWNLOAD_COMMENT_CAPTION.format(text=text)
    if len(value.encode("utf-16-le")) <= 2048:
        return value
    return value.encode("utf-16-le")[:2046].decode("utf-16-le", errors="ignore") + "…"


class TikTokComments:
    def __init__(self, http: PublicHttp):
        self.http = http

    async def resolve(self, link: VideoLink) -> VideoLink:
        if not link.is_short_tiktok:
            return link
        try:
            async with asyncio.timeout(6):
                result = await self.http.get_browser(link.url, 8 * 1024**2)
            resolved = normalize_url(result.url)
            if resolved.platform == "TikTok" and (
                resolved.is_live or re.search(r"/(?:video|photo|story|stories)/[0-9]+", urlsplit(resolved.url).path)
            ):
                return resolved
        except (MediaError, LinkError, TimeoutError):
            logger.info("tiktok_comment_redirect unavailable=true")
        return link

    async def text(self, link: VideoLink) -> str:
        match = re.search(r"/(?:video|photo)/([0-9]+)", urlsplit(link.url).path)
        if not link.comment_id or not match:
            return ""
        query = urlencode(
            {
                "aweme_id": match[1],
                "comment_id": link.comment_id,
                "insert_ids": link.comment_id,
                "focus_state": "true",
                "aid": "1988",
                "count": 20,
                "cursor": 0,
            }
        )
        try:
            async with asyncio.timeout(5):
                result = await self.http.get_browser("https://www.tiktok.com/api/comment/list/?" + query, 2 * 1024**2)
            text = selected_comment(result.body, link.comment_id, match[1])
            if text:
                return text
        except (MediaError, TimeoutError):
            logger.info("tiktok_comment_lookup unavailable=true")
        return ""

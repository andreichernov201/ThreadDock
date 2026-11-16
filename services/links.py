"""Извлечение ссылок и проверка поддерживаемых платформ. Без сетевых запросов."""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from core import texts as T


class LinkError(ValueError):
    """Ссылку нельзя использовать для скачивания."""


class UrlEntity(Protocol):
    type: str
    offset: int
    length: int
    url: str | None


@dataclass(frozen=True)
class VideoLink:
    url: str
    platform: str

    @property
    def cache_key(self) -> str:
        # Versioned: incompatible rendering changes can safely invalidate old media.
        identity = self.url
        parts = urlsplit(self.url)
        if self.platform == "YouTube":
            video_id = parse_qs(parts.query).get("v", [""])[0]
            short = re.fullmatch(r"/(?:shorts|live)/([\w-]+)/?", parts.path, re.ASCII)
            if video_id or short:
                identity = "youtube:" + (video_id or short[1])
        if self.platform == "TikTok":
            match = re.search(r"/(?:video|photo)/(\d+)", parts.path)
            if match:
                identity = f"tiktok:{match[1]}"
        if self.platform == "Instagram":
            match = re.fullmatch(r"/(?:reel|reels|p)/([\w-]+)/?", parts.path, re.ASCII)
            if match:
                identity = f"instagram:{match[1]}"
        if self.platform == "X":
            match = re.search(r"/status/(\d+)", parts.path)
            if match:
                identity = f"x:{match[1]}"
        return hashlib.sha256(("media-v1:" + identity).encode()).hexdigest()


# Только точные имена: youtube.com.evil.example и произвольные поддомены запрещены.
PLATFORM_HOSTS = {
    "TikTok": {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"},
    "Instagram": {"instagram.com", "www.instagram.com", "m.instagram.com"},
    "YouTube": {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"},
    "VK": {"vk.com", "www.vk.com", "m.vk.com", "vkvideo.ru", "www.vkvideo.ru", "m.vkvideo.ru"},
    "X": {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"},
    "Vimeo": {"vimeo.com", "www.vimeo.com"},
}
_URL_PATTERN = re.compile(r"\b(?:https?://|www\.)[^\s<>\"'«»]+", re.IGNORECASE)
_TRACKING_PARAMS = {"si", "feature", "fbclid", "igsh", "igshid", "gclid"}


def _trim_punctuation(url: str) -> str:
    url = url.rstrip(".,!?;:")
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        while url.endswith(closing) and url.count(closing) > url.count(opening):
            url = url[:-1].rstrip(".,!?;:")
    return url


def extract_urls(text: str, entities: Sequence[UrlEntity] = ()) -> list[str]:
    """Учитывает скрытые ссылки, UTF-16 offsets Telegram, порядок и дубли."""
    encoded = text.encode("utf-16-le")
    found: list[tuple[int, str]] = []
    covered: list[tuple[int, int]] = []
    for entity in entities:
        if entity.type not in {"url", "text_link"}:
            continue
        start, end = entity.offset, entity.offset + entity.length
        if start < 0 or end <= start or end * 2 > len(encoded):
            continue
        try:
            visible_url = encoded[start * 2 : end * 2].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        url = entity.url if entity.type == "text_link" else visible_url
        if url:
            found.append((start, url))
            covered.append((start, end))

    for match in _URL_PATTERN.finditer(text):
        start = len(text[: match.start()].encode("utf-16-le")) // 2
        end = start + len(match.group().encode("utf-16-le")) // 2
        if not any(start < right and end > left for left, right in covered):
            found.append((start, _trim_punctuation(match.group())))

    # dict сохраняет порядок и убирает повторения.
    return list(dict.fromkeys(url for _, url in sorted(found, key=lambda item: item[0])))


def normalize_url(raw_url: str) -> VideoLink:
    """Разрешает известные платформы; DNS и адрес назначения проверяются при соединении."""
    url = raw_url.strip()
    if not url or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise LinkError(T.SERVICES_LINKS_INVALID_CHARACTERS)
    if "\\" in url or len(url) > 8192 or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", url, re.IGNORECASE):
        raise LinkError(T.SERVICES_LINKS_INVALID_URL)
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as error:
        raise LinkError(T.SERVICES_LINKS_INVALID_HOST) from error
    if parts.scheme.lower() not in {"http", "https"}:
        raise LinkError(T.SERVICES_LINKS_WEB_URL_REQUIRED)
    if parts.username is not None or parts.password is not None or port not in {None, 443}:
        raise LinkError(T.SERVICES_LINKS_CREDENTIALS_OR_PORT)
    platform = next((name for name, hosts in PLATFORM_HOSTS.items() if host in hosts), None)
    if platform is None:
        raise LinkError(T.SERVICES_LINKS_UNSUPPORTED_SITE)
    if not parts.path.strip("/"):
        raise LinkError(T.SERVICES_LINKS_PUBLICATION_REQUIRED)
    if platform == "YouTube" and parts.path.rstrip("/") == "/playlist":
        raise LinkError(T.SERVICES_LINKS_PLAYLIST_UNSUPPORTED)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parts.path.strip("/")
        if not re.fullmatch(r"[\w-]+", video_id, flags=re.ASCII):
            raise LinkError(T.SERVICES_LINKS_INVALID_YOUTUBE_SHORT_URL)
        host, path = "www.youtube.com", "/watch"
        query = [("v", video_id)] + [(key, value) for key, value in query if key != "v"]
    else:
        path = parts.path
    if platform == "YouTube":
        if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
            host = "www.youtube.com"
        query = [(key, value) for key, value in query if key not in {"list", "index"}]
    # Пользовательские HTTP-ссылки повышаем до HTTPS. Фрагмент серверу не нужен.
    return VideoLink(urlunsplit(("https", host, path, urlencode(query), "")), platform)

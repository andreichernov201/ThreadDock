"""Bounded public HTTPS requests. Redirects pass the same SSRF guard as yt-dlp."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp

from core import texts as T
from core.errors import MediaError
from services.network import SafeProxy

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


@dataclass(frozen=True)
class HttpResult:
    body: bytes
    url: str
    content_type: str


def check_https_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
            or "\\" in url
            or any(ord(char) < 33 for char in url)
        ):
            raise ValueError
    except ValueError as error:
        raise MediaError(T.SERVICES_HTTP_UNSAFE_MEDIA_URL, code="unsafe_url") from error


class PublicHttp:
    async def __aenter__(self) -> PublicHttp:
        self.proxy = await SafeProxy().__aenter__()
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60, connect=15),
            trust_env=False,
            headers={"User-Agent": USER_AGENT},
            connector=aiohttp.TCPConnector(limit=16),
        )
        return self

    async def __aexit__(self, *args) -> None:
        await self.session.close()
        await self.proxy.__aexit__(*args)

    async def get_browser(self, url: str, limit: int) -> HttpResult:
        """Bounded browser-compatible TLS for public metadata behind bot protection.

        The proxy and explicit redirect checks remain mandatory. This client has
        no operator cookies, browser profile, environment proxies or persistent jar.
        """
        try:
            from curl_cffi.requests import AsyncSession, RequestsError
        except ImportError:
            return await self.get(url, limit)
        try:
            async with AsyncSession(proxy=self.proxy.url, impersonate="chrome", trust_env=False) as session:
                for _ in range(8):
                    check_https_url(url)
                    response = await session.get(url, timeout=8, allow_redirects=False, stream=True)
                    try:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location:
                                break
                            url = urljoin(url, location)
                            continue
                        if response.status_code >= 400:
                            raise MediaError(
                                T.SERVICES_HTTP_SOURCE_DENIED,
                                code="source_http",
                                retryable=response.status_code in {429, 500, 502, 503, 504},
                            )
                        body = bytearray()
                        async for chunk in response.aiter_content():
                            body.extend(chunk)
                            if len(body) > limit:
                                raise MediaError(T.SERVICES_HTTP_SOURCE_TOO_LARGE, code="file_too_large")
                        if not body:
                            raise MediaError(T.SERVICES_HTTP_EMPTY_FILE, code="empty_response", retryable=True)
                        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        return HttpResult(bytes(body), url, content_type)
                    finally:
                        await response.aclose()
            raise MediaError(T.SERVICES_HTTP_TOO_MANY_REDIRECTS, code="redirect_limit")
        except (RequestsError, TimeoutError) as error:
            raise MediaError(T.SERVICES_HTTP_SOURCE_UNAVAILABLE, code="network", retryable=True) from error

    async def get(self, url: str, limit: int, *, destination: Path | None = None) -> HttpResult:
        file = None
        try:
            for _ in range(8):
                check_https_url(url)
                async with self.session.get(url, proxy=self.proxy.url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            break
                        url = urljoin(url, location)
                        continue
                    if response.status >= 400:
                        raise MediaError(
                            T.SERVICES_HTTP_SOURCE_DENIED,
                            code="source_http",
                            retryable=response.status in {429, 500, 502, 503, 504},
                        )
                    if response.content_length is not None and response.content_length > limit:
                        raise MediaError(T.SERVICES_HTTP_SOURCE_TOO_LARGE, code="file_too_large")
                    if destination:
                        file = destination.open("wb")
                    body = bytearray()
                    size = 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > limit:
                            raise MediaError(T.SERVICES_HTTP_SOURCE_TOO_LARGE, code="file_too_large")
                        if file:
                            file.write(chunk)
                        else:
                            body.extend(chunk)
                    if size == 0:
                        raise MediaError(T.SERVICES_HTTP_EMPTY_FILE, code="empty_response", retryable=True)
                    return HttpResult(bytes(body), url, response.content_type)
            raise MediaError(T.SERVICES_HTTP_TOO_MANY_REDIRECTS, code="redirect_limit")
        except (aiohttp.ClientError, TimeoutError) as error:
            raise MediaError(T.SERVICES_HTTP_SOURCE_UNAVAILABLE, code="network", retryable=True) from error
        finally:
            if file:
                file.close()

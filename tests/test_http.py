import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.errors import MediaError
from services.http import PublicHttp


class Response:
    def __init__(self, *, status=200, body=b"body", headers=None, content_length=None):
        self.status, self.headers = status, headers or {}
        self.content_length, self.content_type, self.body = content_length, "text/html", body
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        yield self.body


class HttpTests(unittest.IsolatedAsyncioTestCase):
    def client(self, responses):
        client = PublicHttp()
        client.proxy = SimpleNamespace(url="http://guarded-proxy")
        client.session = SimpleNamespace(get=Mock(side_effect=responses))
        return client

    async def test_https_redirect_uses_guard_for_every_request(self):
        client = self.client([Response(status=302, headers={"Location": "https://cdn.example/image"}), Response()])
        result = await client.get("https://site.example/post", 100)
        self.assertEqual(result.url, "https://cdn.example/image")
        for call in client.session.get.call_args_list:
            self.assertEqual(call.kwargs["proxy"], "http://guarded-proxy")
            self.assertFalse(call.kwargs["allow_redirects"])

    async def test_http_redirect_is_blocked_before_request(self):
        client = self.client([Response(status=302, headers={"Location": "http://127.0.0.1/secret"})])
        with self.assertRaises(MediaError):
            await client.get("https://site.example/post", 100)
        self.assertEqual(client.session.get.call_count, 1)

    async def test_unknown_content_length_is_still_bounded(self):
        client = self.client([Response(body=b"x" * 101)])
        with self.assertRaisesRegex(MediaError, "большой"):
            await client.get("https://site.example/post", 100)

    async def test_temporary_source_status_is_retryable(self):
        client = self.client([Response(status=503)])
        with self.assertRaises(MediaError) as captured:
            await client.get("https://site.example/post", 100)
        self.assertTrue(captured.exception.retryable)


@unittest.skipUnless(importlib.util.find_spec("curl_cffi"), "curl_cffi media extra is not installed")
class BrowserHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_metadata_uses_proxy_and_blocks_unsafe_redirect(self):
        response = SimpleNamespace(
            status_code=302, headers={"Location": "http://127.0.0.1/private"}, aclose=AsyncMock()
        )
        session = SimpleNamespace(get=AsyncMock(return_value=response))
        context = AsyncMock()
        context.__aenter__.return_value = session
        client = PublicHttp()
        client.proxy = SimpleNamespace(url="http://guard")
        with patch("curl_cffi.requests.AsyncSession", return_value=context) as factory:
            with self.assertRaises(MediaError):
                await client.get_browser("https://site.example/metadata", 100)
        factory.assert_called_once_with(proxy="http://guard", impersonate="chrome", trust_env=False)
        session.get.assert_awaited_once()
        response.aclose.assert_awaited_once()

    async def test_browser_stream_is_bounded_and_closed(self):
        async def chunks():
            yield b"x" * 101

        response = SimpleNamespace(status_code=200, headers={}, aiter_content=chunks, aclose=AsyncMock())
        session = SimpleNamespace(get=AsyncMock(return_value=response))
        context = AsyncMock()
        context.__aenter__.return_value = session
        client = PublicHttp()
        client.proxy = SimpleNamespace(url="http://guard")
        with patch("curl_cffi.requests.AsyncSession", return_value=context):
            with self.assertRaises(MediaError):
                await client.get_browser("https://site.example/metadata", 100)
        response.aclose.assert_awaited_once()

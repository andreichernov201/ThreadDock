import unittest
from types import SimpleNamespace
from unittest.mock import Mock

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

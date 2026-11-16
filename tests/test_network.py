import asyncio
import socket
import unittest
from unittest.mock import AsyncMock, patch

from services.network import SafeProxy, UnsafeAddressError, check_public_ip, resolve_public_addresses


def dns_answer(ip, family=socket.AF_INET):
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))


class PublicIpTests(unittest.TestCase):
    def test_non_public_and_transition_addresses_are_blocked(self):
        for ip in [
            "127.0.0.1",
            "10.0.0.1",
            "192.168.1.1",
            "172.16.0.1",
            "169.254.169.254",
            "0.0.0.0",
            "100.64.0.1",
            "224.0.0.1",
            "::1",
            "::",
            "fe80::1",
            "fc00::1",
            "::ffff:127.0.0.1",
            "2002:7f00:1::",
            "2001::1",
            "ff02::1",
            "fe80::1%1",
        ]:
            with self.subTest(ip=ip), self.assertRaises(UnsafeAddressError):
                check_public_ip(ip)

    def test_public_ipv4_and_ipv6_are_allowed(self):
        check_public_ip("8.8.8.8")
        check_public_ip("2606:4700:4700::1111")


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_dns_answer_is_rejected(self):
        loop = asyncio.get_running_loop()
        with patch.object(
            loop, "getaddrinfo", AsyncMock(return_value=[dns_answer("8.8.8.8"), dns_answer("127.0.0.1")])
        ):
            with self.assertRaises(UnsafeAddressError):
                await resolve_public_addresses("public.example", 443)

    async def test_only_https_port_is_allowed(self):
        for port in (0, 22, 80, 8080):
            with self.subTest(port=port), self.assertRaises(UnsafeAddressError):
                await resolve_public_addresses("public.example", port)

    async def test_connect_uses_checked_ip_without_second_hostname_lookup(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[dns_answer("8.8.8.8")])) as dns:
            with patch(
                "services.network.asyncio.open_connection", AsyncMock(return_value=("reader", "writer"))
            ) as connect:
                self.assertEqual(await SafeProxy()._connect("public.example", 443), ("reader", "writer"))
                connect.assert_awaited_once_with("8.8.8.8", 443, family=socket.AF_INET)
                dns.assert_awaited_once()

    async def proxy_request(self, proxy, target, auth=True, method="CONNECT"):
        reader = asyncio.StreamReader()
        writer = MemoryWriter()
        header = f"{method} {target} HTTP/1.1\r\n"
        if auth:
            header += f"Proxy-Authorization: {proxy._authorization}\r\n"
        reader.feed_data((header + "\r\n").encode())
        reader.feed_eof()
        await proxy._handle_client(reader, writer)
        return bytes(writer.data)

    async def test_proxy_denies_private_target_before_connect(self):
        proxy = SafeProxy()
        with patch("services.network.resolve_public_addresses", AsyncMock(side_effect=UnsafeAddressError)):
            response = await self.proxy_request(proxy, "127.0.0.1:443")
            self.assertIn(b"403 Forbidden", response)

    async def test_proxy_rejects_missing_authentication(self):
        proxy = SafeProxy()
        with patch.object(proxy, "_connect", AsyncMock()) as connect:
            self.assertIn(b"403 Forbidden", await self.proxy_request(proxy, "public.example:443", auth=False))
            connect.assert_not_awaited()

    async def test_proxy_rejects_plain_http(self):
        proxy = SafeProxy()
        with patch.object(proxy, "_connect", AsyncMock()) as connect:
            response = await self.proxy_request(proxy, "http://127.0.0.1/secret", method="GET")
            self.assertIn(b"403 Forbidden", response)
            connect.assert_not_awaited()

    async def test_proxy_establishes_public_tunnel(self):
        proxy = SafeProxy()
        remote_reader = asyncio.StreamReader()
        remote_reader.feed_eof()
        remote_writer = MemoryWriter()
        with patch.object(proxy, "_connect", AsyncMock(return_value=(remote_reader, remote_writer))):
            self.assertIn(b"200 Connection Established", await self.proxy_request(proxy, "public.example:443"))
        self.assertTrue(remote_writer.closed)
        self.assertEqual(proxy._clients, set())

    async def test_proxy_lifecycle_closes_listener(self):
        server = AsyncMock()
        server.close = lambda: None
        with patch("services.network.asyncio.start_server", AsyncMock(return_value=server)):
            async with SafeProxy():
                pass
        server.wait_closed.assert_awaited_once()

    async def test_shutdown_cancels_clients_before_waiting_for_server(self):
        proxy = SafeProxy()
        client = asyncio.create_task(asyncio.sleep(60))
        proxy._clients.add(client)
        server = AsyncMock()
        server.close = lambda: None

        async def wait_closed():
            self.assertTrue(client.done())

        server.wait_closed.side_effect = wait_closed
        with patch("services.network.asyncio.start_server", AsyncMock(return_value=server)):
            async with proxy:
                pass
        self.assertTrue(client.cancelled())

    async def test_new_tunnel_rechecks_dns_after_rebinding(self):
        loop = asyncio.get_running_loop()
        with patch.object(
            loop,
            "getaddrinfo",
            AsyncMock(
                side_effect=[
                    [dns_answer("8.8.8.8")],
                    [dns_answer("10.0.0.1")],
                ]
            ),
        ):
            self.assertEqual(await resolve_public_addresses("public.example", 443), [(socket.AF_INET, "8.8.8.8")])
            with self.assertRaises(UnsafeAddressError):
                await resolve_public_addresses("public.example", 443)


class MemoryWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


if __name__ == "__main__":
    unittest.main()
